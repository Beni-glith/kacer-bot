#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KACER BOT — Final (normalize menu choices 01-09 -> 1-9, full features)
- Output tools dibuffer → dikirim 1 pesan setelah idle 5s
- Menu numerik jadi inline button (callback mengirim 1,2,... untuk 01-09)
- Prompt tanpa newline tetap terdeteksi
- Auto-handle "Press enter to continue..." (kirim newline otomatis)
- Forward semua user text langsung ke stdin tool (tanpa konfirmasi chat)
- Hilangkan spam "✅ Input dikirim"
- Multi-user sessions, per-user logs di ~/bot_logs/<user_id>.log
"""

import asyncio
import importlib.util
import os
import re
import signal
import site
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -------------------------
# CONFIG
# -------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not set. Set environment variable BOT_TOKEN or edit script.")
    sys.exit(1)

HOME_DIR = os.path.expanduser("/data/data/com.termux/files/home")
TOOLS_DIR = os.path.join(HOME_DIR, "me-cli")
TOOLS_FILE = os.path.join(TOOLS_DIR, "main.py")

BUFFER_FLUSH_IDLE = 5.0   # seconds
TG_MAX = 3800

MENU_ITEM_RE = re.compile(r"^\s*(\d+)\.\s*(.+)$")
PROMPT_KEYWORDS_RE = re.compile(r"(?i)\b(pilih|enter|family|kode|otp|nomor|number|pin|masuk)\b")
ENDS_WITH_COLON_RE = re.compile(r".+:\s*$")
ONLY_DIGITS_RE = re.compile(r"^\d{1,8}$")

LOG_DIR = os.path.join(HOME_DIR, "bot_logs")
os.makedirs(LOG_DIR, exist_ok=True)

# -------------------------
# Session dataclass
# -------------------------
@dataclass
class Session:
    user_id: int
    chat_id: int
    proc: Optional[asyncio.subprocess.Process] = None
    stdin_writer: Optional[asyncio.StreamWriter] = None

    partial: str = ""
    buffer_lines: List[str] = field(default_factory=list)
    menu_items: List[Tuple[str, str]] = field(default_factory=list)

    last_output_time: float = 0.0
    awaiting_input: bool = False
    last_prompt_time: Optional[float] = None
    input_prompt_text: Optional[str] = None

    reader_task: Optional[asyncio.Task] = None
    flusher_task: Optional[asyncio.Task] = None

    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_running(self) -> bool:
        return self.proc is not None and (self.proc.returncode is None)

SESSIONS: Dict[int, Session] = {}

# -------------------------
# Helpers: logging, keyboards, sending
# -------------------------
def _log(session: Session, msg: str, tag: str = "INFO"):
    try:
        path = os.path.join(LOG_DIR, f"{session.user_id}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{tag}] {msg}\n")
    except Exception:
        pass

def main_bot_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("▶️ Jalankan Tools", callback_data="bot_run")],
        [InlineKeyboardButton("ℹ️ Status Session", callback_data="bot_status")],
        [
            InlineKeyboardButton("📋 List Sessions", callback_data="bot_list"),
            InlineKeyboardButton("🛑 Stop", callback_data="bot_stop"),
            InlineKeyboardButton("♻️ Reset Session", callback_data="bot_reset"),
        ],
    ]
    return InlineKeyboardMarkup(kb)

def normalize_choice(num: str) -> str:
    """
    Normalize choice for callback/send:
    - If num == '00' keep '00' (special)
    - If num starts with '0' and is 2 chars (01-09) -> strip leading zero -> '1'..'9'
    - Else return num as-is
    """
    n = num.strip()
    if n == "00":
        return "00"
    if len(n) == 2 and n.startswith("0"):
        # '01' -> '1'
        try:
            return str(int(n))
        except Exception:
            return n
    # for other cases (e.g., '10','99','0') keep as-is
    return n

def menu_kb_from_items(items: List[Tuple[str, str]]) -> InlineKeyboardMarkup:
    buttons = []
    for num, label in items:
        display_num = normalize_choice(num)  # show normalized to avoid confusion
        txt = f"{display_num}. {label if len(label) <= 28 else label[:25] + '...'}"
        cb = f"menu_choice|{normalize_choice(num)}"
        buttons.append(InlineKeyboardButton(txt, callback_data=cb))
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    back_num = find_back_num(items)
    rows.append([
        InlineKeyboardButton("⬅️ Menu Utama (Tools)", callback_data=f"menu_back|{normalize_choice(back_num)}"),
        InlineKeyboardButton("❌ Batal", callback_data="menu_cancel"),
    ])
    return InlineKeyboardMarkup(rows)

def find_back_num(menu_items: List[Tuple[str, str]]) -> str:
    for num, label in menu_items:
        if "kemb" in label.lower() or "kembali" in label.lower() or "utama" in label.lower() or "back" in label.lower():
            return num
    for num, _ in menu_items:
        if num in ("00", "0"):
            return num
    for num, _ in menu_items:
        if num == "99":
            return num
    return menu_items[0][0] if menu_items else "99"

async def send_long_message(bot, chat_id: int, text: str, reply_markup=None):
    if not text:
        return
    start = 0
    l = len(text)
    first = True
    while start < l:
        end = min(l, start + TG_MAX)
        if end < l:
            nl = text.rfind("\n", start, end)
            if nl != -1 and nl > start:
                end = nl + 1
        chunk = text[start:end]
        try:
            if first and reply_markup:
                await bot.send_message(chat_id=chat_id, text=chunk, reply_markup=reply_markup)
                first = False
            else:
                await bot.send_message(chat_id=chat_id, text=chunk)
        except Exception:
            # ignore send errors (best effort)
            pass
        start = end

# -------------------------
# Subprocess env helper
# -------------------------
def build_subprocess_env():
    env = os.environ.copy()
    try:
        site_paths = []
        try:
            sp = site.getsitepackages()
            if isinstance(sp, list):
                site_paths.extend(sp)
        except Exception:
            pass
        try:
            usp = site.getusersitepackages()
            if usp:
                site_paths.append(usp)
        except Exception:
            pass
        try:
            spec = importlib.util.find_spec("dotenv")
            if spec and spec.origin:
                pkg_parent = os.path.dirname(os.path.dirname(spec.origin))
                if pkg_parent and pkg_parent not in site_paths:
                    site_paths.append(pkg_parent)
        except Exception:
            pass
        site_paths = [p for p in dict.fromkeys(site_paths) if p and os.path.isdir(p)]
        if site_paths:
            existing = env.get("PYTHONPATH", "")
            new = os.pathsep.join(site_paths)
            env["PYTHONPATH"] = new + (os.pathsep + existing if existing else "")
    except Exception:
        pass
    return env

# -------------------------
# Subprocess lifecycle
# -------------------------
async def start_tool(session: Session, context: ContextTypes.DEFAULT_TYPE):
    async with session.lock:
        if session.is_running():
            await context.bot.send_message(chat_id=session.chat_id, text="⚠️ Tool sudah berjalan.", reply_markup=main_bot_kb())
            return
        if not os.path.isfile(TOOLS_FILE):
            await context.bot.send_message(chat_id=session.chat_id, text=f"❌ File tool tidak ditemukan: {TOOLS_FILE}", reply_markup=main_bot_kb())
            return
        python = sys.executable or "python3"
        env = build_subprocess_env()
        try:
            session.proc = await asyncio.create_subprocess_exec(
                python, TOOLS_FILE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=TOOLS_DIR if TOOLS_DIR else None,
                env=env,
            )
        except Exception as e:
            await context.bot.send_message(chat_id=session.chat_id, text=f"❌ Gagal menjalankan tool: {e}", reply_markup=main_bot_kb())
            return

        session.stdin_writer = session.proc.stdin
        session.partial = ""
        session.buffer_lines.clear()
        session.menu_items.clear()
        session.last_output_time = asyncio.get_event_loop().time()
        session.awaiting_input = False
        session.last_prompt_time = None
        session.input_prompt_text = None

        session.reader_task = asyncio.create_task(_reader_loop(session, context))
        session.flusher_task = asyncio.create_task(_flusher_loop(session, context))

        await context.bot.send_message(chat_id=session.chat_id, text=f"▶️ Tool dijalankan (PID {getattr(session.proc,'pid','?')})", reply_markup=main_bot_kb())

async def stop_tool(session: Session, context: ContextTypes.DEFAULT_TYPE):
    async with session.lock:
        if not session.is_running():
            await context.bot.send_message(chat_id=session.chat_id, text="ℹ️ Tidak ada tool berjalan.", reply_markup=main_bot_kb())
            return
        try:
            session.proc.terminate()
            try:
                await asyncio.wait_for(session.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                session.proc.kill()
                await session.proc.wait()
        except Exception:
            pass
        await _cancel_tasks(session)
        await context.bot.send_message(chat_id=session.chat_id, text="🛑 Tool dihentikan.", reply_markup=main_bot_kb())

async def reset_session(session: Session, context: ContextTypes.DEFAULT_TYPE):
    await stop_tool(session, context)
    session.partial = ""
    session.buffer_lines.clear()
    session.menu_items.clear()
    session.awaiting_input = False
    session.last_prompt_time = None
    session.input_prompt_text = None
    await context.bot.send_message(chat_id=session.chat_id, text="♻️ Session di-reset.", reply_markup=main_bot_kb())

async def _cancel_tasks(session: Session):
    for t in (session.reader_task, session.flusher_task):
        if t:
            try:
                t.cancel()
            except Exception:
                pass
    session.reader_task = None
    session.flusher_task = None
    session.proc = None
    session.stdin_writer = None

# -------------------------
# Reader & processing
# -------------------------
async def _reader_loop(session: Session, context: ContextTypes.DEFAULT_TYPE):
    try:
        reader = session.proc.stdout
        while True:
            data = await reader.read(1024)
            if not data:
                break
            s = data.decode(errors="replace")
            session.partial += s
            session.last_output_time = asyncio.get_event_loop().time()

            # process full lines
            while "\n" in session.partial:
                line, session.partial = session.partial.split("\n", 1)
                await _process_line(session, line.rstrip("\r"), is_partial=False)

            # check partial for prompt-like content (no newline)
            p = session.partial.strip()
            if p:
                if ENDS_WITH_COLON_RE.match(p) or PROMPT_KEYWORDS_RE.search(p) or ONLY_DIGITS_RE.match(p):
                    await _process_line(session, p, is_partial=True)
                    session.partial = ""
        # EOF leftover
        if session.partial:
            await _process_line(session, session.partial.rstrip("\r\n"), is_partial=True)
            session.partial = ""
        # final flush
        await _flush_buffer_and_menu(session, context)
        # notify completion
        await send_long_message(context.bot, session.chat_id, "🔚 Proses tool selesai.", reply_markup=main_bot_kb())
    except asyncio.CancelledError:
        return
    except Exception as e:
        try:
            await send_long_message(context.bot, session.chat_id, f"⚠️ Reader error: {e}", reply_markup=main_bot_kb())
        except Exception:
            pass

async def _process_line(session: Session, line: str, is_partial: bool = False):
    if not line:
        return

    low = line.lower()
    if "press enter to continue" in low or "press any key to continue" in low:
        # auto-send newline to let tool continue
        if session.stdin_writer:
            try:
                session.stdin_writer.write(b"\n")
                await session.stdin_writer.drain()
                _log(session, "AUTO-SENT Enter", "AUTO")
            except Exception:
                pass
        return

    m = MENU_ITEM_RE.match(line)
    if m:
        num = m.group(1).strip()
        label = m.group(2).strip()
        session.menu_items.append((num, label))
        _log(session, f"MENU {num} -> {label}", "OUT")
        return

    session.buffer_lines.append(line)
    session.last_output_time = asyncio.get_event_loop().time()
    _log(session, line, "OUT")

    # detect prompt-like content (including partials)
    is_prompt = is_partial or ENDS_WITH_COLON_RE.match(line) or PROMPT_KEYWORDS_RE.search(line) or ONLY_DIGITS_RE.match(line)
    if is_prompt:
        session.awaiting_input = True
        session.last_prompt_time = asyncio.get_event_loop().time()
        session.input_prompt_text = line
        _log(session, f"PROMPT detected: {line}", "PROMPT")
        # do not flush here — flusher will send combined message after idle

# -------------------------
# Flusher
# -------------------------
async def _flusher_loop(session: Session, context: ContextTypes.DEFAULT_TYPE):
    try:
        while True:
            await asyncio.sleep(0.5)
            if session.buffer_lines or session.menu_items:
                idle = asyncio.get_event_loop().time() - session.last_output_time
                if idle >= BUFFER_FLUSH_IDLE:
                    await _flush_buffer_and_menu(session, context)
    except asyncio.CancelledError:
        return

async def _flush_buffer_and_menu(session: Session, context: ContextTypes.DEFAULT_TYPE):
    async with session.send_lock:
        if not session.buffer_lines and not session.menu_items:
            return
        lines = session.buffer_lines.copy()
        session.buffer_lines.clear()
        menu = session.menu_items.copy()
        session.menu_items.clear()

        text = "\n".join(lines).strip()
        if menu:
            menu_text = "\n".join([f"{n}. {l}" for n, l in menu])
            if text:
                full = text + "\n\n📋 Menu:\n" + menu_text
            else:
                full = "📋 Menu:\n" + menu_text
            kb = menu_kb_from_items(menu)
            await send_long_message(context.bot, session.chat_id, full, reply_markup=kb)
            _log(session, f"SENT combined (menu) len={len(full)}", "SEND")
        else:
            if text:
                await send_long_message(context.bot, session.chat_id, text, reply_markup=None)
                _log(session, f"SENT combined text len={len(text)}", "SEND")

# -------------------------
# Callbacks & message handlers
# -------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    await q.answer()  # no popup text, just acknowledge
    user = update.effective_user
    chat_id = update.effective_chat.id
    s = SESSIONS.setdefault(user.id, Session(user_id=user.id, chat_id=chat_id))
    data = q.data

    # Bot-level commands
    if data == "bot_run":
        await start_tool(s, context)
        return
    if data == "bot_stop":
        await stop_tool(s, context)
        return
    if data == "bot_reset":
        await reset_session(s, context)
        return
    if data == "bot_status":
        info = f"Status:\n- Running: {s.is_running()}\n- Awaiting input: {s.awaiting_input}"
        try:
            await q.edit_message_text(info, reply_markup=main_bot_kb())
        except Exception:
            pass
        return
    if data == "bot_list":
        parts = [f"{uid} – running={ss.is_running()}" for uid, ss in SESSIONS.items()]
        try:
            await q.edit_message_text("📋 Sessions:\n" + ("\n".join(parts) if parts else "Tidak ada session"), reply_markup=main_bot_kb())
        except Exception:
            pass
        return

    # menu_choice: send normalized (no leading zero for 01-09)
    if data.startswith("menu_choice|"):
        num = data.split("|", 1)[1]
        if s.is_running() and s.stdin_writer:
            try:
                s.stdin_writer.write((num + "\n").encode())
                await s.stdin_writer.drain()
                s.awaiting_input = False
                s.last_prompt_time = None
                s.input_prompt_text = None
                _log(s, f"SENT via menu_choice: {num}", "IN")
            except Exception:
                pass
        return

    # menu_back: similar to menu_choice
    if data.startswith("menu_back|"):
        back_num = data.split("|", 1)[1]
        if s.is_running() and s.stdin_writer:
            try:
                s.stdin_writer.write((back_num + "\n").encode())
                await s.stdin_writer.drain()
                s.awaiting_input = False
                s.last_prompt_time = None
                s.input_prompt_text = None
                _log(s, f"SENT via menu_back: {back_num}", "IN")
            except Exception:
                pass
        return

    # cancel
    if data == "menu_cancel":
        try:
            await q.edit_message_text("❌ Menu ditutup.", reply_markup=main_bot_kb())
        except Exception:
            pass
        s.menu_items.clear()
        s.awaiting_input = False
        return

# text handler forwards raw input whenever tool running; no confirmation to avoid spam
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    s = SESSIONS.setdefault(user.id, Session(user_id=user.id, chat_id=chat_id))

    if s.is_running() and s.stdin_writer:
        try:
            s.stdin_writer.write((text + "\n").encode())
            await s.stdin_writer.drain()
            s.awaiting_input = False
            s.last_prompt_time = None
            s.input_prompt_text = None
            _log(s, f"USER->TOOL: {text}", "IN")
            # intentionally do NOT send a chat confirmation message to avoid spam
        except Exception:
            # if sending fails, notify user minimally
            try:
                await update.message.reply_text("❌ Gagal mengirim input ke tool.", reply_markup=main_bot_kb())
            except Exception:
                pass
    else:
        await update.message.reply_text("❌ Tool belum berjalan. Tekan ▶️ Jalankan Tools.", reply_markup=main_bot_kb())

# start command
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    SESSIONS.setdefault(user.id, Session(user_id=user.id, chat_id=update.effective_chat.id))
    await update.message.reply_text("🚀 Selamat datang di KACER BOT — gunakan tombol untuk memulai.", reply_markup=main_bot_kb())

# -------------------------
# Entrypoint
# -------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))

    app.run_polling(allowed_updates=["message", "callback_query"], stop_signals=(signal.SIGINT, signal.SIGTERM))

if __name__ == "__main__":
    main()
