import asyncio
import json
import time
from typing import Optional

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

# TextPart 用于向单轮请求注入动态上下文（AstrBot 4.23+ 提供）
# 老版本没有这个模块时降级：不注入，其它功能不受影响
try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None

START_KEYWORDS = ("陪我听", "一起听歌")
STOP_KEYWORDS = ("不听了", "别放了", "结束陪听", "不用陪了")

DEFAULT_SYSTEM_PROMPT = (
    "你是陪我听歌的朋友。"
    "当我说到某首歌时，不要复读歌名，不要用'这是一首……的歌曲'这种腔调，"
    "也不要说'很好听''很经典'这类空话。"
    "用两三句话说你听到它想到的具体画面、情绪或某个瞬间，像朋友随口聊天。"
    "不知道的信息不要编，尤其不要编歌词。"
)


class _Session:
    """单个会话的陪听状态（仅保存在内存中，重启即失效）"""

    def __init__(self, umo: str):
        self.umo = umo
        self.active = True
        self.last_key: Optional[str] = None
        self.current_track: Optional[dict] = None
        self.last_playing_at = time.time()
        self.task: Optional[asyncio.Task] = None


class ListenWithMe(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.sessions: dict[str, _Session] = {}
        logger.info("[陪我听歌] 插件已加载")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def terminate(self):
        for s in list(self.sessions.values()):
            s.active = False
            if s.task and not s.task.done():
                s.task.cancel()
        self.sessions.clear()

    # ------------------------------------------------------------------
    # 权限：默认只允许私聊，可选白名单
    # ------------------------------------------------------------------
    def _allowed(self, event: AstrMessageEvent) -> bool:
        if self.config.get("only_private_chat", True) and event.get_group_id():
            return False
        allow = self.config.get("allowed_users") or []
        if allow:
            sender = str(event.get_sender_id())
            return sender in {str(x).strip() for x in allow}
        return True

    # ------------------------------------------------------------------
    # 对话上下文：读取最近的聊天记录
    # ------------------------------------------------------------------
    async def _recent_context(self, umo: str) -> list[dict]:
        limit = max(0, int(self.config.get("context_messages", 12) or 0))
        if limit == 0:
            return []
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return []
            conv = await cm.get_conversation(umo, cid)
            if not conv or not conv.history:
                return []
            history = json.loads(conv.history)
        except Exception as e:
            logger.debug(f"[陪我听歌] 读取对话历史失败: {e}")
            return []

        out: list[dict] = []
        for m in history[-limit:]:
            if not isinstance(m, dict):
                continue
            role, content = m.get("role"), m.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                out.append({"role": role, "content": content})
        return out

    # ------------------------------------------------------------------
    # 消息入口：开始 / 结束
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if not text:
            return

        if not self._allowed(event):
            return

        umo = event.unified_msg_origin

        if any(w in text for w in STOP_KEYWORDS):
            if umo in self.sessions:
                await self._stop(umo, say="好，不听了。想听的时候再叫我。")
                event.stop_event()
            return

        if any(w in text for w in START_KEYWORDS):
            if umo in self.sessions:
                yield event.plain_result("已经在陪你听了，你那边换歌我就说一句。")
                event.stop_event()
                return

            if not self.config.get("lastfm_user") or not self.config.get("lastfm_api_key"):
                yield event.plain_result(
                    "还差配置：请在插件设置里填好 Last.fm 用户名和 API Key。"
                )
                event.stop_event()
                return

            self._start(umo)
            yield event.plain_result(
                "好，我陪你听。换歌了我会说一句，想聊现在这首随时问我。"
            )
            event.stop_event()

    # ------------------------------------------------------------------
    # 动态上下文注入：让每一轮对话都知道"此刻在听什么"
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def inject_now_playing(self, event: AstrMessageEvent, req):
        if TextPart is None:
            return
        s = self.sessions.get(event.unified_msg_origin)
        if not s or not s.current_track:
            return
        t = s.current_track
        if not t.get("nowplaying"):
            return
        try:
            req.extra_user_content_parts.append(
                TextPart(
                    text=(
                        "<now_playing>\n"
                        f"你正在陪用户听歌，用户此刻播放的是：{t['name']}"
                        + (f" - {t['artist']}" if t.get("artist") else "")
                        + (f"（专辑：{t['album']}）" if t.get("album") else "")
                        + "\n用户可能会直接问起这首歌，请自然地结合上下文回答。\n"
                        "</now_playing>"
                    )
                )
            )
        except Exception as e:
            logger.debug(f"[陪我听歌] 注入当前曲目失败: {e}")

    # ------------------------------------------------------------------
    # LLM 工具：用户随时问"我在听什么"时按需查询
    # ------------------------------------------------------------------
    @filter.llm_tool(name="get_now_playing")
    async def get_now_playing(self, event: AstrMessageEvent) -> str:
        """查询用户此刻正在听的歌曲（数据来源 Last.fm）。

        当用户询问"我在听什么""现在放的这首歌是什么""刚才那首"之类的问题时调用。
        会返回歌名、歌手和专辑；若用户没有在播放，会明确说明。
        """
        track = await self._fetch_now_playing()
        if track is None:
            return "查询失败：Last.fm 未配置，或网络请求异常。"
        if not track.get("nowplaying"):
            return "用户当前没有在播放音乐（Last.fm 上没有正在播放的记录）。"
        return (
            f"用户正在听：{track['name']}"
            + (f" - {track['artist']}" if track.get("artist") else "")
            + (f"（专辑：{track['album']}）" if track.get("album") else "")
        )

    # ------------------------------------------------------------------
    # 轮询逻辑
    # ------------------------------------------------------------------
    def _start(self, umo: str):
        s = _Session(umo)
        self.sessions[umo] = s
        s.task = asyncio.create_task(self._loop(s))

    async def _loop(self, s: _Session):
        interval = max(30, int(self.config.get("poll_interval", 90) or 90))
        idle_timeout = max(60, int(self.config.get("idle_timeout", 600) or 600))

        try:
            first = True
            while s.active:
                if not first:
                    await asyncio.sleep(interval)
                first = False
                if not s.active:
                    break

                track = await self._fetch_now_playing()
                if track is None:
                    continue

                s.current_track = track

                if track["nowplaying"]:
                    s.last_playing_at = time.time()
                    key = f"{track['name']}|{track['artist']}"
                    if key != s.last_key:
                        s.last_key = key
                        await self._comment(s, track)
                else:
                    if time.time() - s.last_playing_at > idle_timeout:
                        await self._stop(
                            s.umo, say="好像有一阵没在听了，我先歇着。想听随时喊我。"
                        )
                        return

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[陪我听歌] 轮询异常: {e}")
            await self._stop(s.umo, say="")

    async def _fetch_now_playing(self) -> Optional[dict]:
        user = self.config.get("lastfm_user", "")
        api_key = self.config.get("lastfm_api_key", "")
        if not user or not api_key:
            return None

        params = {
            "method": "user.getrecenttracks",
            "user": user,
            "api_key": api_key,
            "format": "json",
            "limit": 1,
        }
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    "https://ws.audioscrobbler.com/2.0/", params=params, timeout=15
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"[陪我听歌] Last.fm 返回 {resp.status}")
                        return None
                    data = await resp.json()
        except Exception as e:
            # 不要把 api_key 打进日志
            logger.warning(f"[陪我听歌] 请求 Last.fm 失败: {type(e).__name__}")
            return None

        tracks = (data.get("recenttracks") or {}).get("track") or []
        if isinstance(tracks, dict):
            tracks = [tracks]
        if not tracks:
            return {"nowplaying": False, "name": "", "artist": "", "album": ""}

        t = tracks[0]
        attr = t.get("@attr") or {}
        return {
            "nowplaying": attr.get("nowplaying") == "true",
            "name": t.get("name", ""),
            "artist": (t.get("artist") or {}).get("#text", ""),
            "album": (t.get("album") or {}).get("#text", ""),
            "url": t.get("url", ""),
        }

    # ------------------------------------------------------------------
    # 生成并发送一句话（带对话上下文）
    # ------------------------------------------------------------------
    async def _comment(self, s: _Session, track: dict):
        name, artist = track["name"], track["artist"]

        # 关键：把最近的聊天记录一起交给模型，让它接得上话
        context = await self._recent_context(s.umo)

        prompt = (
            f"（系统提示：我这边刚切到了《{name}》"
            + (f"，{artist}" if artist else "")
            + "。这不是我发的一条消息，而是你察觉到的变化。"
            "请结合我们刚才在聊的内容，自然地接一句，两三句以内。"
            "如果刚才的话题和音乐无关，可以轻轻带到这首歌上，不要生硬转折。）"
        )

        text = ""
        try:
            prov = await self.context.get_using_provider_async(umo=s.umo)
            if prov:
                resp = await prov.text_chat(
                    prompt=prompt,
                    context=context,
                    system_prompt=self.config.get("system_prompt")
                    or DEFAULT_SYSTEM_PROMPT,
                )
                text = (resp.completion_text or "").strip()
        except Exception as e:
            logger.warning(f"[陪我听歌] 调用模型失败: {e}")

        if not text:
            text = f"🎵 {name}" + (f" - {artist}" if artist else "")

        try:
            await self.context.send_message(s.umo, MessageChain().message(text))
        except Exception as e:
            logger.error(f"[陪我听歌] 发送失败: {e}")
            return

        # 写入对话历史，保证你接着聊"这首"时上下文连贯
        await self._remember(s.umo, text)

    async def _remember(self, umo: str, text: str):
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return
            conv = await cm.get_conversation(umo, cid)
            if not conv:
                return
            history = json.loads(conv.history) if conv.history else []
            history.append({"role": "assistant", "content": text})
            await cm.update_conversation(umo, cid, history=history[-40:])
        except Exception as e:
            logger.debug(f"[陪我听歌] 写入对话历史失败（不影响使用）: {e}")

    # ------------------------------------------------------------------
    # 结束
    # ------------------------------------------------------------------
    async def _stop(self, umo: str, say: str = ""):
        s = self.sessions.pop(umo, None)
        if s:
            s.active = False
            if s.task and not s.task.done() and s.task is not asyncio.current_task():
                s.task.cancel()
        if say:
            try:
                await self.context.send_message(umo, MessageChain().message(say))
            except Exception as e:
                logger.warning(f"[陪我听歌] 发送结束语失败: {e}")
