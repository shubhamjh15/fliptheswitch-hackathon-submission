"""Generate the animated SVGs used by the README (banner + incident replay).

Run: python scripts/make_assets.py
GitHub renders CSS animations in SVGs loaded through <img>, so these play inline.
"""

from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "assets"
OUT.mkdir(exist_ok=True)

FONT = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Helvetica,Arial,sans-serif"


def banner() -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="320" viewBox="0 0 1200 320" role="img" aria-label="SNITCH: prod is on fire, SNITCH already knows whose commit did it">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0b0d10"/><stop offset="1" stop-color="#14202a"/></linearGradient>
  <linearGradient id="t" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#8fc6cf"/><stop offset="1" stop-color="#ff6b5b"/></linearGradient>
  <pattern id="g" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0H0V40" fill="none" stroke="#1f262e" stroke-width="1"/></pattern>
  <style>
    .pulse{{animation:p 1.4s ease-in-out infinite}} @keyframes p{{0%,100%{{opacity:.25;r:7}}50%{{opacity:1;r:11}}}}
    .flip{{animation:f 4s ease-in-out infinite}} @keyframes f{{0%,45%{{transform:translateX(0)}}55%,100%{{transform:translateX(30px)}}}}
    .fade{{animation:fd 4s ease-in-out infinite}} @keyframes fd{{0%,45%{{fill:#3a434d}}55%,100%{{fill:#5bd69a}}}}
    .rat{{animation:b 2.2s ease-in-out infinite;transform-origin:150px 160px}} @keyframes b{{0%,100%{{transform:rotate(-4deg)}}50%{{transform:rotate(4deg)}}}}
  </style>
</defs>
<rect width="1200" height="320" fill="url(#bg)"/><rect width="1200" height="320" fill="url(#g)" opacity=".5"/>
<text x="150" y="190" font-size="110" text-anchor="middle" class="rat">🐀</text>
<text x="260" y="170" font-family="{SANS}" font-weight="800" font-size="104" fill="url(#t)" letter-spacing="-2">SNITCH</text>
<text x="264" y="218" font-family="{SANS}" font-size="27" fill="#e8edf2">Prod is on fire. SNITCH already knows whose commit did it.</text>
<text x="264" y="256" font-family="{FONT}" font-size="17" fill="#8a96a3">AI agents + humans, one Switch room · detect → investigate → approve → rollback → postmortem</text>
<circle cx="1110" cy="70" r="9" fill="#ff6b5b" class="pulse"/>
<text x="1090" y="75" font-family="{FONT}" font-size="15" fill="#ff6b5b" text-anchor="end">INCIDENT OPEN</text>
<g transform="translate(1000 250)"><rect width="74" height="38" rx="19" fill="#1b232b" stroke="#2c4a50"/><circle cx="19" cy="19" r="13" fill="#8fc6cf" class="flip"/></g>
<text x="1090" y="275" font-family="{FONT}" font-size="14" fill="#8a96a3" text-anchor="end">built on Switch</text>
</svg>
"""


LINES = [
    ("#ff6b5b", "snitch-sentinel", "🚨 INC-1 opened · checkout-service degraded · error rate 48% on v2.14.0"),
    ("#8fc6cf", "snitch-detective", "🐀 SNITCHING: it was v2.14.0 (c0ffee7 by @priya) · 38/38 errors · 99% sure"),
    ("#8fc6cf", "snitch-detective", "   KeyError: 'JPY' · first error 2s after the deploy went out"),
    ("#e5b85b", "snitch-fixer", "🔧 Rollback plan: v2.14.0 → v2.13.2 · needs human sign-off"),
    ("#d7b8ff", "priya", "@snitch-fixer approve INC-1"),
    ("#e5b85b", "snitch-fixer", "⏪ Approved by priya. Rolling back… 🟢 v2.13.2 healthy, 0% errors"),
    ("#5bd69a", "snitch-sentinel", "✅ INC-1 resolved · time to recover 1.4 min"),
    ("#8fc6cf", "snitch-scribe", "📝 Postmortem saved as room document postmortem-inc-1"),
]


def incident() -> str:
    step, total = 1.5, 1.5 * len(LINES) + 4
    rows, css = [], []
    for i, (color, who, text) in enumerate(LINES):
        y = 96 + i * 38
        start = i * step / total * 100
        end = (i * step + 0.4) / total * 100
        css.append(f".l{i}{{opacity:0;animation:a{i} {total}s linear infinite}} "
                   f"@keyframes a{i}{{0%,{start:.2f}%{{opacity:0;transform:translateY(6px)}}{end:.2f}%,94%{{opacity:1;transform:none}}100%{{opacity:0}}}}")
        rows.append(f'<g class="l{i}"><text x="28" y="{y}" font-family="{FONT}" font-size="15" font-weight="700" fill="{color}">{escape(who)}</text>'
                    f'<text x="215" y="{y}" font-family="{FONT}" font-size="15" fill="#e8edf2">{escape(text)}</text></g>')
    h = 96 + len(LINES) * 38 + 10
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{h}" viewBox="0 0 1200 {h}" role="img" aria-label="Animated replay of a SNITCH incident in a Switch room">
<style>{"".join(css)}</style>
<rect width="1200" height="{h}" rx="14" fill="#0b0d10" stroke="#1f262e"/>
<rect width="1200" height="48" rx="14" fill="#11151a"/><rect y="30" width="1200" height="18" fill="#11151a"/>
<circle cx="28" cy="24" r="6" fill="#ff5f56"/><circle cx="50" cy="24" r="6" fill="#ffbd2e"/><circle cx="72" cy="24" r="6" fill="#27c93f"/>
<text x="600" y="29" font-family="{FONT}" font-size="14" fill="#8a96a3" text-anchor="middle"># snitch-checkout-service-incidents  ·  Switch room</text>
{"".join(rows)}
</svg>
"""


def flow() -> str:
    boxes = [("🚨", "Sentinel", "detect"), ("🕵️", "Detective", "investigate"), ("🔧", "Fixer", "propose"),
             ("🧑", "HUMAN", "approve"), ("⏪", "Fixer", "roll back"), ("📝", "Scribe", "postmortem")]
    parts, w = [], 1200 / len(boxes)
    for i, (ico, name, verb) in enumerate(boxes):
        x = i * w + 12
        human = name == "HUMAN"
        stroke = "#ff6b5b" if human else "#2c4a50"
        parts.append(f'<g><rect x="{x}" y="30" width="{w-24}" height="130" rx="12" fill="#11151a" stroke="{stroke}" stroke-width="{2 if human else 1}"/>'
                     f'<text x="{x+(w-24)/2}" y="80" font-size="36" text-anchor="middle">{ico}</text>'
                     f'<text x="{x+(w-24)/2}" y="115" font-family="{SANS}" font-weight="700" font-size="19" fill="{"#ff6b5b" if human else "#e8edf2"}" text-anchor="middle">{name}</text>'
                     f'<text x="{x+(w-24)/2}" y="140" font-family="{FONT}" font-size="14" fill="#8a96a3" text-anchor="middle">{verb}</text></g>')
        if i < len(boxes) - 1:
            parts.append(f'<path d="M{x+w-22} 95 H{x+w-2}" stroke="#8fc6cf" stroke-width="2" stroke-dasharray="4 4" class="d"/>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="190" viewBox="0 0 1200 190" role="img" aria-label="SNITCH workflow: detect, investigate, propose, human approves, roll back, postmortem">
<style>.d{{animation:m 1s linear infinite}} @keyframes m{{to{{stroke-dashoffset:-16}}}}</style>
<rect width="1200" height="190" rx="14" fill="#0b0d10"/>
{"".join(parts)}
<text x="600" y="182" font-family="{FONT}" font-size="13" fill="#8a96a3" text-anchor="middle">every hand-off is an @mention in the Switch room, so any human can step in at any step</text>
</svg>
"""


if __name__ == "__main__":
    (OUT / "banner.svg").write_text(banner(), encoding="utf-8")
    (OUT / "incident.svg").write_text(incident(), encoding="utf-8")
    (OUT / "flow.svg").write_text(flow(), encoding="utf-8")
    print("wrote", [p.name for p in OUT.glob("*.svg")])
