"""모션 인포그래픽 렌더러: 장면을 프레임 단위로 그려 ffmpeg에 흘려보낸다.

표현 요소
  - 숫자 카운트업, 막대가 차오르는 월별 거래량, 선이 그려지는 중위가 추이, 순위 막대 경주
  - 요소별 등장(슬라이드+페이드), 구절 단위 자막, 영상 진행 막대
  - 배경: 테마별 그라데이션 또는 공개 사진(천천히 움직이는 팬)
테마(THEMES)는 성과 실험(learn.py)의 실험 변수다.
"""
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from .analyze import won
from .render import W, H, _wrap, font
from .tts import FFMPEG

FPS = 30

THEMES = {
    "midnight": dict(top=(15, 23, 42), bottom=(30, 58, 138), text="#ffffff", muted="#cbd5e1", accent="#fbbf24",
                     up="#f87171", down="#60a5fa", chip="#1d4ed8", bar="#3b82f6", grid="#475569", cap=(0, 0, 0, 165)),
    "ember": dict(top=(24, 24, 27), bottom=(68, 32, 20), text="#fafaf9", muted="#d6d3d1", accent="#fb923c",
                  up="#f87171", down="#7dd3fc", chip="#9a3412", bar="#ea580c", grid="#57534e", cap=(0, 0, 0, 170)),
    "paper": dict(top=(248, 245, 238), bottom=(226, 232, 240), text="#0f172a", muted="#475569", accent="#b45309",
                  up="#b91c1c", down="#1d4ed8", chip="#0f172a", bar="#0f766e", grid="#94a3b8", cap=(15, 23, 42, 215)),
    "forest": dict(top=(6, 30, 28), bottom=(13, 74, 64), text="#f0fdf4", muted="#bbf7d0", accent="#fde047",
                   up="#fca5a5", down="#93c5fd", chip="#047857", bar="#34d399", grid="#3f6f66", cap=(0, 0, 0, 165)),
}


def ease(p: float) -> float:
    p = max(0.0, min(1.0, p))
    return 1 - (1 - p) ** 3


def _gradient(th) -> Image.Image:
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(th["top"][i] + (th["bottom"][i] - th["top"][i]) * t) for i in range(3)))
    return img


def is_light(th) -> bool:
    r, g, b = th["top"]
    return 0.299 * r + 0.587 * g + 0.114 * b > 140


def readable_theme(th, photo_bg) -> dict:
    """밝은 테마(paper)라도 배경 영상(ClipBackground)은 항상 어둡게 깔리므로 글자색을 밝게 바꾼다.

    11-25 송파 숏츠: paper 테마 진한 남색 제목·블로그 주소가 어두운 야경 영상 위에서 거의 안 보였음.
    사진 배경은 테마의 위쪽 색으로 덮어 밝게 만들기 때문에 그대로 둔다."""
    if not isinstance(photo_bg, ClipBackground) or not is_light(th):
        return th
    return dict(th, text="#ffffff", muted="#e2e8f0", accent="#fbbf24", chip="#0f172a", cap=(0, 0, 0, 175))


def _photo_bg(photo: Path, th) -> Image.Image:
    """사진을 세로 화면보다 조금 크게 맞추고 어둡게 덮는다 (팬 여유 12%)."""
    im = Image.open(photo).convert("RGB")
    scale = max(W * 1.12 / im.width, H / im.height)
    im = im.resize((int(im.width * scale) + 1, int(im.height * scale) + 1))
    veil = Image.new("RGB", im.size, th["top"])
    return Image.blend(im, veil, 0.62)


# ---------------------------------------------------------------- 숫자 파싱 (카운트업용)

def _parse_count(s: str):
    m = re.fullmatch(r"([\d,]+)건", s.strip())
    return int(m.group(1).replace(",", "")) if m else None


def _parse_won(s: str):
    m = re.fullmatch(r"(?:(\d+)억)?\s*(?:([\d,]+)만원|원)?", s.strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(m.group(1) or 0) * 10000 + int((m.group(2) or "0").replace(",", ""))


def counting(value: str, p: float) -> str:
    """p(0~1)에 따라 0에서 최종값까지 올라가는 문자열. 해석할 수 없는 값은 그대로."""
    if p >= 1:
        return value
    n = _parse_count(value)
    if n is not None:
        return f"{int(n * p):,}건"
    v = _parse_won(value)
    if v is not None and won(v) == value:
        step = 1000 if v >= 20000 else 100
        return won(max(step, int(v * p) // step * step))
    return value


# ---------------------------------------------------------------- 그리기 도구

def _sprite(text: str, f, fill, width: int = W - 140) -> Image.Image:
    """가운데 정렬 글 묶음을 투명 이미지로."""
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    lines = _wrap(probe, text, f, width)
    lh = int(f.size * 1.25)
    img = Image.new("RGBA", (W, lh * len(lines) + 12), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text(((W - d.textlength(line, font=f)) / 2, i * lh), line, font=f, fill=fill)
    return img


def _put(frame: Image.Image, sp: Image.Image, y: int, t: float, delay: float = 0.0, dur: float = 0.45, dy: int = 46):
    """등장 애니메이션: 아래에서 올라오며 나타난다."""
    p = ease((t - delay) / dur)
    if p <= 0:
        return
    if p >= 1:
        frame.paste(sp, (0, y), sp)
        return
    mask = sp.getchannel("A").point(lambda a: int(a * p))
    frame.paste(sp, (0, y + int((1 - p) * dy)), mask)


def _center_text(d, y, text, f, fill):
    d.text(((W - d.textlength(text, font=f)) / 2, y), text, font=f, fill=fill)


def _chunks(narration: str, size: int = 15) -> list[str]:
    """자막을 구절 단위로 나눈다 (한 번에 한 구절만 크게 보여 줌)."""
    out, cur = [], ""
    for word in narration.split():
        if cur and len(cur) + len(word) + 1 > size:
            out.append(cur)
            cur = word
        else:
            cur = (cur + " " + word).strip()
    if cur:
        out.append(cur)
    return out or [""]


class Scene:
    def __init__(self, kind, headline, narration, facts, trend, th, site_title, site_url, seconds,
                 photo_bg=None, credit=None, intro=True):
        th = readable_theme(th, photo_bg)
        self.kind, self.facts, self.trend, self.th = kind, facts, trend or [], th
        self.seconds, self.intro = seconds, intro
        self.site_title, self.site_url = site_title, site_url
        self.photo_bg = photo_bg
        self.grad = None if photo_bg is not None else _gradient(th)
        # 고정 층: 브랜드, 지역·월, 사진 출처
        self.static = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(self.static)
        tag = site_title
        tw = d.textlength(tag, font=font(40)) + 60
        d.rounded_rectangle(((W - tw) / 2, 150, (W + tw) / 2, 215), radius=32, fill=th["chip"])
        _center_text(d, 158, tag, font(40), "#ffffff")
        _center_text(d, 240, f"{facts['region']} · {facts['month']}", font(44, False), th["muted"])
        if credit:
            d.text((60, H - 150), credit[:60], font=font(26, False), fill=th["muted"])
        self.headline = _sprite(headline, font(84), th["text"])
        self.y0 = 380 + self.headline.height + 40
        # 자막 구절과 시작 시각 (글자 수 비례)
        self.caps = _chunks(narration)
        total = sum(len(c) for c in self.caps) or 1
        speak = max(0.5, seconds - 0.35)
        acc, self.cap_at = 0, []
        for c in self.caps:
            self.cap_at.append(speak * acc / total)
            acc += len(c)
        self.cap_sprites = [self._caption(c) for c in self.caps]

    def _caption(self, text: str) -> Image.Image:
        f = font(64)
        probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        while f.size > 44 and len(_wrap(probe, text, f, W - 160)) > 1 and " " not in text:
            f = font(f.size - 6)             # 긴 한 단어(단지명)는 글자를 줄여 한 줄에
        lines = _wrap(probe, text, f, W - 160)
        h = len(lines) * 84 + 44
        img = Image.new("RGBA", (W, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        wmax = max(d.textlength(line, font=f) for line in lines) + 90
        d.rounded_rectangle(((W - wmax) / 2, 0, (W + wmax) / 2, h), radius=30, fill=self.th["cap"])
        for i, line in enumerate(lines):
            _center_text(d, 20 + i * 84, line, f, "#ffffff")
        return img

    # ------------------------------------------------------------ 장면별 본문

    def _body(self, fr: Image.Image, d: ImageDraw.ImageDraw, t: float):
        th, facts, y = self.th, self.facts, self.y0
        k = self.kind
        if k == "hook":
            _put(fr, _cached(self, "r", lambda: _sprite(facts["region"], font(170), th["accent"])), y + 60, t, 0.0)
            _put(fr, _cached(self, "m", lambda: _sprite(f"{facts['month']} 아파트 실거래", font(70), th["text"])),
                 y + 300, t, 0.25)
            w = int((W - 360) * ease((t - 0.3) / 0.8))
            d.rounded_rectangle((180, y + 420, 180 + max(w, 8), y + 432), radius=6, fill=th["accent"])
        elif k == "volume":
            p = ease((t - 0.15) / 0.9)
            _center_text(d, y + 10, counting(facts["trade_count"], p), font(170), th["accent"])
            chg = facts["count_change"]
            color = th["up"] if chg.startswith("+") else th["down"] if chg.startswith("-") else th["muted"]
            _put(fr, _cached(self, "c", lambda: _sprite(f"전월 대비 {chg}", font(68), color)), y + 235, t, 0.9)
            self._bars(d, y + 380, t - 0.5)
        elif k == "price":
            p = ease((t - 0.15) / 0.9)
            _center_text(d, y, counting(facts["median_price"], p), font(112), th["accent"])
            chg = facts["median_change"]
            color = th["up"] if chg.startswith("+") else th["down"] if chg.startswith("-") else th["muted"]
            _put(fr, _cached(self, "c", lambda: _sprite(f"전월 대비 {chg}", font(60), color)), y + 160, t, 0.9)
            self._line(d, y + 300, t - 0.5)
        elif k == "top_deal":
            p = ease((t - 0.15) / 1.0)
            _center_text(d, y + 30, counting(facts["top_price"], p), font(124), th["accent"])
            _put(fr, _cached(self, "a", lambda: _sprite(facts["top_apt"], font(76), th["text"])), y + 215, t, 0.7)
            meta = f"{facts.get('top_apt_umd', '')} · {facts['top_area']} · {facts['top_floor']}".strip(" ·")
            _put(fr, _cached(self, "d", lambda: _sprite(meta, font(52, False), th["muted"])), y + 430, t, 0.95)
            _put(fr, _cached(self, "e", lambda: _sprite(f"계약일 {facts['top_date']}", font(46, False), th["muted"])),
                 y + 510, t, 1.1)
        elif k == "busy":
            self._ranks(d, y + 30, t - 0.2)
        elif k == "outro":
            _put(fr, _cached(self, "s", lambda: _sprite(self.site_title, font(100), th["accent"])), y + 60, t, 0.0)
            _put(fr, _cached(self, "b", lambda: _sprite("단지별 실거래 표는 블로그에서", font(64), th["text"])),
                 y + 230, t, 0.3)
            _put(fr, _cached(self, "u", lambda: _sprite(self.site_url.replace("https://", ""), font(50, False),
                                                        th["muted"])), y + 340, t, 0.55)

    def _bars(self, d, top, t):
        """월별 거래량 막대: 왼쪽부터 차례로 차오르고 마지막(이번 달) 막대를 강조."""
        data = self.trend[-8:]
        if len(data) < 2:
            return
        th, left, right, base = self.th, 110, W - 110, top + 400   # 자막 상자(약 1400px~) 위에서 끝나게
        mx = max(n for _, n, _ in data) or 1
        slot = (right - left) / len(data)
        d.line([(left, base), (right, base)], fill=th["grid"], width=3)
        for i, (ym, n, _) in enumerate(data):
            p = ease((t - i * 0.09) / 0.55)
            h = int(320 * n / mx * p)
            x0 = left + i * slot + slot * 0.16
            last = i == len(data) - 1
            if h > 2:
                d.rounded_rectangle((x0, base - h, x0 + slot * 0.68, base), radius=10,
                                    fill=th["accent"] if last else th["bar"])
            lab = f"{int(ym[4:])}월"
            f = font(34, last)
            d.text((x0 + slot * 0.34 - d.textlength(lab, font=f) / 2, base + 14), lab, font=f,
                   fill=th["text"] if last else th["muted"])
            if last and p >= 1:
                v = f"{n:,}건"
                d.text((x0 + slot * 0.34 - d.textlength(v, font=font(38)) / 2, base - h - 54), v, font=font(38),
                       fill=th["accent"])

    def _line(self, d, top, t):
        """월별 중위가 선: 왼쪽에서 오른쪽으로 그려진다."""
        data = self.trend[-12:]
        if len(data) < 2:
            return
        th, left, right, base, hgt = self.th, 120, W - 120, top + 470, 380
        vals = [m for _, _, m in data]
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1
        pts = [(left + (right - left) * i / (len(data) - 1), base - 40 - (hgt - 80) * (v - lo) / span)
               for i, v in enumerate(vals)]
        for g in range(4):
            gy = base - hgt * g / 3
            d.line([(left, gy), (right, gy)], fill=th["grid"], width=1)
        prog = ease(t / 1.4) * (len(pts) - 1)
        seg, frac = int(prog), prog - int(prog)
        shown = pts[: seg + 1]
        if seg < len(pts) - 1:
            a, b = pts[seg], pts[seg + 1]
            shown.append((a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac))
        if len(shown) >= 2:
            d.line(shown, fill=th["accent"], width=9, joint="curve")
        hx, hy = shown[-1]
        d.ellipse((hx - 15, hy - 15, hx + 15, hy + 15), fill=th["accent"], outline=th["text"], width=4)
        for i in (0, len(data) - 1):
            lab = f"{data[i][0][2:4]}.{int(data[i][0][4:])}"
            d.text((pts[i][0] - d.textlength(lab, font=font(32, False)) / 2, base + 14), lab, font=font(32, False),
                   fill=th["muted"])
        if prog >= len(pts) - 1:
            v = won(vals[-1])
            d.text((right - d.textlength(v, font=font(38)), base - hgt - 52), v, font=font(38), fill=th["text"])

    def _ranks(self, d, top, t):
        """거래 많은 단지: 가로 막대가 차례로 자라는 순위표."""
        th, facts = self.th, self.facts
        rows = [(facts[f"busy{i}_apt"], facts[f"busy{i}_count"]) for i in (1, 2, 3) if f"busy{i}_apt" in facts]
        if not rows:
            return
        mx = max(_parse_count(c) or 1 for _, c in rows)
        for i, (name, cnt) in enumerate(rows):
            p = ease((t - i * 0.28) / 0.6)
            if p <= 0:
                continue
            y = top + i * 215
            f = font(54)
            label = name
            while d.textlength(label, font=f) > W - 330 and len(label) > 4:
                label = label[:-2] + "…"
            d.text((110, y), f"{i + 1}", font=font(72), fill=th["accent"])
            d.text((190, y + 8), label, font=f, fill=th["text"])
            full = (W - 440) * (_parse_count(cnt) or 1) / mx
            d.rounded_rectangle((190, y + 92, 190 + max(14, full * p), y + 132), radius=14,
                                fill=th["accent"] if i == 0 else th["bar"])
            if p >= 1:
                d.text((190 + full + 20, y + 84), cnt, font=font(46), fill=th["text"])

    # ------------------------------------------------------------ 프레임

    def frame(self, t: float, progress: float) -> Image.Image:
        if isinstance(self.photo_bg, ClipBackground):
            fr = self.photo_bg.next()
        elif self.photo_bg is not None:
            pan = int((self.photo_bg.width - W) * min(1.0, t / max(self.seconds, 0.1)))
            fr = self.photo_bg.crop((pan, 0, pan + W, H))
        else:
            fr = self.grad.copy()
        fr.paste(self.static, (0, 0), self.static)
        tt = t if self.intro else t + 5.0   # 첫 장면은 처음부터 다 보이게 (표지·첫 프레임)
        _put(fr, self.headline, 380, tt, 0.0, 0.4)
        d = ImageDraw.Draw(fr)
        self._body(fr, d, tt)
        idx = max(i for i, at in enumerate(self.cap_at) if at <= t)
        cap = self.cap_sprites[idx]
        _put(fr, cap, 1500 - cap.height // 2, t if self.intro or idx else t + 5.0, self.cap_at[idx], 0.18, 18)
        d = ImageDraw.Draw(fr)
        d.rectangle((0, 0, int(W * progress), 10), fill=self.th["accent"])
        return fr


def _cached(scene: Scene, key: str, make):
    store = scene.__dict__.setdefault("_sprites", {})
    if key not in store:
        store[key] = make()
    return store[key]


def render_scene(scene: Scene, audio: Path, out: Path, t_offset: float, total: float) -> None:
    """장면 한 개를 프레임 단위로 그려 음성과 함께 mp4로 인코딩."""
    n = max(1, round(scene.seconds * FPS))
    proc = subprocess.Popen([
        FFMPEG, "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-i", str(audio),
        "-filter_complex", "[0:v]format=yuv420p[v];[1:a]apad,aresample=44100[a]",
        "-map", "[v]", "-map", "[a]", "-t", f"{n / FPS:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "128k", "-ac", "2", str(out),
    ], stdin=subprocess.PIPE)
    try:
        for i in range(n):
            t = i / FPS
            proc.stdin.write(scene.frame(t, (t_offset + t) / total).tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg 인코딩 실패")


class ClipBackground:
    """스톡 영상 클립을 세로 화면 크기로 잘라 어둡게 만든 뒤 프레임을 차례로 준다 (끝나면 처음부터)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.proc = None
        self.frames_read = 0

    def _open(self):
        self.proc = subprocess.Popen([
            FFMPEG, "-loglevel", "error", "-i", str(self.path), "-an",
            "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},"
                   "eq=saturation=0.75,colorchannelmixer=rr=0.5:gg=0.5:bb=0.5,gblur=sigma=2",  # 글자가 잘 보이게 절반 밝기
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)

    def next(self) -> Image.Image:
        size = W * H * 3
        for _ in range(2):
            if self.proc is None:
                self._open()
            buf = self.proc.stdout.read(size)
            if len(buf) == size:
                self.frames_read += 1
                return Image.frombytes("RGB", (W, H), buf)
            self.close()
            if self.frames_read == 0:
                break
        raise RuntimeError("배경 클립을 읽을 수 없음")

    def close(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None


def load_clip(path):
    if not path or not Path(path).exists():
        return None
    try:
        c = ClipBackground(Path(path))
        c.next()
        return c
    except Exception:
        return None


def load_photo(path: Path | None, theme: dict):
    if path and Path(path).exists():
        try:
            return _photo_bg(Path(path), theme)
        except Exception:
            return None
    return None
