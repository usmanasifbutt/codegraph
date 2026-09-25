"""Render docs/codegraph-vs-claude-code-{light,dark}.svg from bench results.

  uv run python -m bench.make_chart --name backend --subtitle "my-repo · 70 Python files"

Small multiples (one scale per measure, never a dual axis); colors are the validated
categorical slots 1-2 for each theme.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from bench.common import RESULTS
from bench.report import load, summarise

OUT = Path(__file__).parent.parent / "docs"
THEMES = {
    "light": dict(surface="#fcfcfb", border="#e4e3de", primary="#0b0b0b", secondary="#52514e",
                  muted="#8a8984", grid="#e4e3de", s1="#2a78d6", s2="#eb6834"),
    "dark": dict(surface="#1a1a19", border="#33332f", primary="#ffffff", secondary="#c3c2b7",
                 muted="#8f8e86", grid="#33332f", s1="#3987e5", s2="#d95926"),
}  # fmt: skip


def _model(model: str) -> str:
    """`openai/gpt-4o-mini` -> `gpt-4o-mini`; `claude-sonnet-5` -> `Sonnet 5`."""
    name = model.split("/")[-1].split(",")[0]
    return name.removeprefix("claude-").replace("-", " ").title() if "claude" in name else name


def _money(v: float) -> str:
    return f"${v:.5f}" if v < 0.01 else f"${v:.3f}"


def panels(a, b) -> list[tuple]:
    """(title, codegraph value, Claude Code value, formatter) per measure."""
    return [
        ("Cost per question (USD) · lower is better", a.cost_per_q, b.cost_per_q, _money),
        ("Tokens per question · lower is better", a.tokens_per_q, b.tokens_per_q,
         lambda v: f"{v / 1000:.1f}k"),
        (f"Correct answers (of {a.n}) · higher is better", a.correct, b.correct,
         lambda v, n=a.n: f"{v:g}/{n}"),
        ("Latency per question (s) · lower is better", a.secs_per_q, b.secs_per_q,
         lambda v: f"{v:.1f}s"),
    ]  # fmt: skip


W, H = 820, 330
PW, PH = 380, 104
LABEL_W, BAR_MAX, BAR_H, GAP = 96, 196, 18, 10


def bar(x, y, w, h, color):
    """Bar anchored at the baseline (square) with a 4px rounded data end."""
    r = min(4, w / 2, h / 2)
    if w <= 0:
        return ""
    return (
        f'<path d="M{x},{y} H{x + w - r} Q{x + w},{y} {x + w},{y + r} V{y + h - r} '
        f'Q{x + w},{y + h} {x + w - r},{y + h} H{x} Z" fill="{color}"/>'
    )


def svg(t, panel_list, subtitle, a, b):
    font = "font-family=\"-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif\""
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'role="img" aria-labelledby="t d" {font}>',
        '<title id="t">codegraph vs Claude Code</title>',
        f'<desc id="d">{subtitle}. codegraph vs Claude Code per question: cost '
        f"{_money(a.cost_per_q)} vs {_money(b.cost_per_q)}; tokens {a.tokens_per_q / 1000:.1f}k vs "
        f"{b.tokens_per_q / 1000:.1f}k; correct {a.correct}/{a.n} vs {b.correct}/{b.n}; latency "
        f"{a.secs_per_q:.1f} s vs {b.secs_per_q:.1f} s.</desc>",
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="10" fill="{t["surface"]}" '
        f'stroke="{t["border"]}"/>',
        f'<text x="24" y="36" font-size="17" font-weight="600" fill="{t["primary"]}">'
        "codegraph vs Claude Code</text>",
        f'<text x="24" y="58" font-size="12.5" fill="{t["secondary"]}">{subtitle} · codegraph '
        f"on {_model(a.model)}, Claude Code on {_model(b.model)}</text>",
    ]
    # legend (top right)
    lx = W - 250
    for i, (name, key) in enumerate((("codegraph", "s1"), ("Claude Code", "s2"))):
        x = lx + i * 118
        parts.append(f'<rect x="{x}" y="27" width="12" height="12" rx="3" fill="{t[key]}"/>')
        parts.append(
            f'<text x="{x + 18}" y="37.5" font-size="12.5" fill="{t["primary"]}">{name}</text>'
        )
    for i, (title, va, vb, fmt) in enumerate(panel_list):
        px = 24 + (i % 2) * (PW + 12)
        py = 82 + (i // 2) * (PH + 12)
        scale = BAR_MAX / max(va, vb)
        parts.append(
            f'<text x="{px}" y="{py + 18}" font-size="12.5" font-weight="600" '
            f'fill="{t["primary"]}">{title}</text>'
        )
        base_x = px + LABEL_W
        y0 = py + 38
        axis_bottom = y0 + 2 * BAR_H + GAP + 8
        parts.append(
            f'<line x1="{base_x}" y1="{y0 - 8}" x2="{base_x}" y2="{axis_bottom}" '
            f'stroke="{t["grid"]}" stroke-width="1"/>'
        )
        for j, (name, v, key) in enumerate((("codegraph", va, "s1"), ("Claude Code", vb, "s2"))):
            y = y0 + j * (BAR_H + GAP)
            w = max(v * scale, 2)
            parts.append(
                f'<text x="{base_x - 10}" y="{y + 13}" font-size="12" text-anchor="end" '
                f'fill="{t["secondary"]}">{name}</text>'
            )
            parts.append(bar(base_x, y, w, BAR_H, t[key]))
            parts.append(
                f'<text x="{base_x + w + 8}" y="{y + 13}" font-size="12" '
                f'font-weight="600" fill="{t["primary"]}">{fmt(v)}</text>'
            )
    parts.append(
        f'<text x="24" y="{H - 14}" font-size="11" fill="{t["muted"]}">Each panel has its own '
        "scale. Claude Code cost is its own reported total_cost_usd (API-equivalent). "
        "codegraph indexing uses no LLM tokens.</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", required=True, help="results folder with codegraph + claude_code")
    ap.add_argument(
        "--subtitle", required=True, help="e.g. 'my-repo · 70 Python files · 8 questions'"
    )
    args = ap.parse_args(argv)
    data = load(RESULTS / args.name)
    a, b = summarise(data["codegraph"]), summarise(data["claude_code"])
    OUT.mkdir(exist_ok=True)
    for mode, t in THEMES.items():
        path = OUT / f"codegraph-vs-claude-code-{mode}.svg"
        path.write_text(svg(t, panels(a, b), args.subtitle, a, b), encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
