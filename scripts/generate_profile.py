#!/usr/bin/env python3
"""Generate a terminal-style SVG profile card from GitHub data and an avatar."""

from __future__ import annotations

import html
import base64
import hashlib
import io
import json
import os
import re
import sys
import calendar
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "profile.json"
OUTPUT_PATH = ROOT / "assets" / "profile-terminal.svg"
README_PATH = ROOT / "README.md"
ASCII_WIDTH = 1000
ASCII_HEIGHT = 58
CARD_WIDTH = 985
CARD_HEIGHT = 530
RIGHT_X = 425
PORTRAIT_X = 15
PORTRAIT_Y = 14
MONOSPACE_WIDTH_RATIO = 0.602
ASCII_RENDER_FONT_SIZE = 6.25
ASCII_RENDER_ROW_HEIGHT = 8.4

# Theme colors — edit these to recolor the generated profile card.
BACKGROUND_FALLBACK_COLOR = "#030712"
BACKGROUND_OVERLAY_COLOR = "#000000"
FONT_COLOR = "#ffffff"
KEY_COLOR = "#f7b267"
VALUE_COLOR = "#d2d6d8"
DOT_COLOR = "#64748b"
SUCCESS_COLOR = "#67e8f9"
ERROR_COLOR = "#f85149"
CLASSIFIED_COLOR = "#b32828"
ORBIT_COLOR = "#d2d6d8"
ORBIT_OPACITY = 0.1
ORBIT_CENTER_COLOR = "#f7b267"
ORBIT_CENTER_OPACITY = 0.2


def api_get(path: str, token: str = "") -> dict | list:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "profile-readme-generator",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def api_graphql(query: str, variables: dict, token: str) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "profile-readme-generator",
    }
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request("https://api.github.com/graphql", data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload["data"]


def download_image(url: str, token: str = "") -> Image.Image:
    headers = {"User-Agent": "profile-readme-generator"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return Image.open(io.BytesIO(response.read())).convert("RGB")


def load_avatar(config: dict, user: dict, token: str) -> Image.Image:
    """Use a configured local avatar for development and GitHub's avatar in Actions."""
    local_avatar = os.getenv("LOCAL_AVATAR") or config.get("local_avatar")
    running_in_actions = os.getenv("GITHUB_ACTIONS", "").lower() == "true"
    force_live_data = os.getenv("PROFILE_LIVE_DATA", "").lower() == "true"
    if local_avatar and not running_in_actions and not force_live_data:
        avatar_path = Path(local_avatar)
        if not avatar_path.is_absolute():
            avatar_path = ROOT / avatar_path
        if not avatar_path.is_file():
            raise FileNotFoundError(f"Local development avatar not found: {avatar_path}")
        print(f"Using local development avatar: {avatar_path.name}")
        with Image.open(avatar_path) as image:
            return image.convert("RGB")
    print("Using GitHub avatar")
    return download_image(user["avatar_url"], token)


def background_data_uri(config: dict) -> str:
    """Create a compact, card-sized JPEG data URI from the configured background."""
    background_name = config.get("background_image")
    if not background_name:
        return ""
    background_path = Path(background_name)
    if not background_path.is_absolute():
        background_path = ROOT / background_path
    if not background_path.is_file():
        raise FileNotFoundError(f"Background image not found: {background_path}")
    with Image.open(background_path) as source:
        background = ImageOps.fit(
            source.convert("RGB"),
            (CARD_WIDTH, CARD_HEIGHT),
            method=Image.Resampling.LANCZOS,
        )
    encoded = io.BytesIO()
    background.save(encoded, format="JPEG", quality=82, optimize=True, progressive=True)
    payload = base64.b64encode(encoded.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{payload}"


def update_readme_cache_key(svg: str) -> str:
    """Give each generated SVG a content-based URL so GitHub cannot show a stale card."""
    cache_key = hashlib.sha256(svg.encode("utf-8")).hexdigest()[:12]
    image_url = f"./assets/profile-terminal.svg?v={cache_key}"
    readme = README_PATH.read_text(encoding="utf-8")
    updated, replacements = re.subn(
        r'\./assets/profile-terminal\.svg(?:\?v=[^"\s>]+)?',
        image_url,
        readme,
    )
    if replacements == 0:
        raise RuntimeError("README does not contain the generated profile image URL")
    if updated != readme:
        README_PATH.write_text(updated, encoding="utf-8", newline="\n")
    return cache_key


def setting_percent(settings: dict, key: str, default: float) -> float:
    return max(0.0, float(settings.get(key, default))) / 100.0


def shift_hue(image: Image.Image, degrees: float) -> Image.Image:
    """Rotate RGB hue by the requested number of degrees."""
    if degrees % 360 == 0:
        return image
    hue, saturation, value = image.convert("HSV").split()
    offset = round((degrees % 360) * 255 / 360)
    hue = hue.point(lambda channel: (channel + offset) % 256)
    return Image.merge("HSV", (hue, saturation, value)).convert("RGB")


def apply_sepia(image: Image.Image, amount: float) -> Image.Image:
    if amount <= 0:
        return image
    sepia = ImageOps.colorize(ImageOps.grayscale(image), "#2b1b0e", "#f4d6a0")
    return Image.blend(image, sepia, min(1.0, amount))


def atkinson_dither(image: Image.Image, levels: int) -> Image.Image:
    """Apply Atkinson error diffusion at the ASCII gradient depth."""
    width, height = image.size
    values = [float(value) for value in image.getdata()]
    quantized = [0] * len(values)
    scale = max(1, levels - 1)
    offsets = ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2))
    for y in range(height):
        for x in range(width):
            index = y * width + x
            old_value = min(255.0, max(0.0, values[index]))
            new_value = round(old_value * scale / 255) * 255 / scale
            quantized[index] = round(new_value)
            error_share = (old_value - new_value) / 8
            for dx, dy in offsets:
                target_x, target_y = x + dx, y + dy
                if 0 <= target_x < width and target_y < height:
                    values[target_y * width + target_x] += error_share
    output = Image.new("L", image.size)
    output.putdata(quantized)
    return output


def ascii_portrait(image: Image.Image, settings: dict) -> list[str]:
    """Process an avatar using asciiart.eu-style controls and map it to characters."""
    width = max(8, int(settings.get("characters", ASCII_WIDTH)))
    height = max(8, int(settings.get("rows", ASCII_HEIGHT)))
    image = ImageOps.fit(
        image.convert("RGB"), (width, height * 2), method=Image.Resampling.LANCZOS
    )
    image = ImageEnhance.Brightness(image).enhance(
        setting_percent(settings, "brightness_percent", 100)
    )
    image = ImageEnhance.Contrast(image).enhance(
        setting_percent(settings, "contrast_percent", 100)
    )
    image = ImageEnhance.Color(image).enhance(
        setting_percent(settings, "saturation_percent", 100)
    )
    image = shift_hue(image, float(settings.get("hue_degrees", 0)))
    grayscale = ImageOps.grayscale(image).convert("RGB")
    image = Image.blend(
        image,
        grayscale,
        min(1.0, setting_percent(settings, "grayscale_percent", 100)),
    )
    image = apply_sepia(image, setting_percent(settings, "sepia_percent", 0))
    image = Image.blend(
        image,
        ImageOps.invert(image),
        min(1.0, setting_percent(settings, "invert_percent", 0)),
    )
    if settings.get("sharpness_enabled", False):
        image = ImageEnhance.Sharpness(image).enhance(float(settings.get("sharpness", 1)))
    if settings.get("edge_detection_enabled", False):
        edges = image.filter(ImageFilter.FIND_EDGES)
        image = Image.blend(
            image, edges, min(1.0, float(settings.get("edge_detection", 1)))
        )
    image = ImageOps.grayscale(image)
    if settings.get("threshold_enabled", False):
        threshold = min(255, max(0, int(settings.get("threshold", 128))))
        image = image.point(lambda value: 255 if value >= threshold else 0)

    ramp = str(settings.get("gradient", "@%#*+=-:. ")) or "@%#*+=-:. "
    ramp += " " * max(0, int(settings.get("space_density", 1)) - 1)
    if str(settings.get("dithering", "")).lower() == "atkinson":
        image = atkinson_dither(image, len(ramp))
    pixels = list(image.getdata())
    rows: list[str] = []
    for y in range(0, image.height, 2):
        row = []
        for x in range(image.width):
            value = (pixels[y * image.width + x] + pixels[(y + 1) * image.width + x]) // 2
            index = min(len(ramp) - 1, (255 - value) * len(ramp) // 256)
            row.append(ramp[index])
        rows.append("".join(row).rstrip())
    return rows


def fetch_languages(repos: list[dict], token: str) -> Counter:
    totals: Counter = Counter()
    for repo in repos:
        if repo.get("fork") or repo.get("private"):
            continue
        try:
            totals.update(api_get(repo["languages_url"].removeprefix("https://api.github.com"), token))
        except (urllib.error.URLError, TimeoutError):
            if repo.get("language"):
                totals[repo["language"]] += 1
    return totals


def language_summary(languages: Counter, limit: int = 5) -> str:
    total = sum(languages.values())
    if not total:
        return "More code coming soon"
    return "  ".join(
        f"{name} {amount / total:.0%}" for name, amount in languages.most_common(limit)
    )


def age_text(created_at: str) -> str:
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    start = created.date()
    end = datetime.now(timezone.utc).date()
    years = end.year - start.year
    if (end.month, end.day) < (start.month, start.day):
        years -= 1
    anniversary_day = min(start.day, calendar.monthrange(start.year + years, start.month)[1])
    anchor = start.replace(year=start.year + years, day=anniversary_day)
    months = (end.year - anchor.year) * 12 + end.month - anchor.month
    if end.day < anchor.day:
        months -= 1
    month_index = anchor.month - 1 + months
    anchor_year = anchor.year + month_index // 12
    anchor_month = month_index % 12 + 1
    anchor_day = min(anchor.day, calendar.monthrange(anchor_year, anchor_month)[1])
    month_anchor = anchor.replace(year=anchor_year, month=anchor_month, day=anchor_day)
    days = (end - month_anchor).days

    def unit(amount: int, word: str) -> str:
        return f"{amount} {word}{'' if amount == 1 else 's'}"

    return f"{unit(years, 'year')}, {unit(months, 'month')}, {unit(days, 'day')}"


def clipped(value: object, width: int = 66) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def display(value: object, fallback: str = "not public") -> str:
    return clipped(value or fallback, 46)


def render_svg(
    config: dict,
    user: dict,
    repos: list[dict],
    languages: Counter,
    portrait: list[str],
    commit_count: int,
    contributed_count: int,
    annual_contributions: int,
) -> str:
    background_uri = background_data_uri(config)
    background_overlay = min(1.0, max(0.0, float(config.get("background_overlay", 0.5))))
    orbit_opacity = min(1.0, max(0.0, float(ORBIT_OPACITY)))
    orbit_center_opacity = min(1.0, max(0.0, float(ORBIT_CENTER_OPACITY)))
    ascii_settings = config.get("ascii", {})
    name = config.get("name") or user.get("name") or user["login"]
    name_parts = name.lower().split()
    terminal_name = config.get("terminal") or (
        f"{name_parts[0]}@{name_parts[-1]}" if len(name_parts) > 1 else user["login"]
    )
    uptime_start = config.get("birthday") or user["created_at"]
    uptime = age_text(uptime_start)
    programming = (
        config.get("languages_programming")
        or config.get("toolchain")
        or ", ".join(name for name, _ in languages.most_common(6))
    )
    portrait_width = max(1.0, float(ascii_settings.get("width", 375)))
    portrait_height = max(1.0, float(ascii_settings.get("height", 492)))
    portrait_characters = max(1, int(ascii_settings.get("characters", ASCII_WIDTH)))
    portrait_rows = max(1, len(portrait))
    natural_width = portrait_characters * ASCII_RENDER_FONT_SIZE * MONOSPACE_WIDTH_RATIO
    natural_height = (
        ASCII_RENDER_FONT_SIZE
        + (portrait_rows - 1) * ASCII_RENDER_ROW_HEIGHT
        + ASCII_RENDER_FONT_SIZE * 0.25
    )

    def text(y: int, content: str, css_class: str = "") -> str:
        class_attr = f' class="{css_class}"' if css_class else ""
        return f'<text x="{RIGHT_X}" y="{y}"{class_attr}>{html.escape(content)}</text>'

    def field_line(y: int, label: str, value: object) -> str:
        shown = clipped(display(value), max(10, 50 - len(label)))
        dots = "." * max(2, 55 - len(label) - len(shown))
        return (
            f'<text x="{RIGHT_X}" y="{y}"><tspan class="cc">. </tspan>'
            f'<tspan class="key">{html.escape(label)}</tspan>:'
            f'<tspan class="cc"> {dots} </tspan>'
            f'<tspan class="value">{html.escape(shown)}</tspan></text>'
        )

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CARD_WIDTH}px" height="{CARD_HEIGHT}px" viewBox="0 0 {CARD_WIDTH} {CARD_HEIGHT}" role="img" aria-labelledby="title desc" font-family="Consolas, ui-monospace, monospace" font-size="16">',
        f'<title id="title">{html.escape(name)} GitHub profile</title>',
        '<desc id="desc">ASCII portrait on the left and developer details and GitHub statistics on the right.</desc>',
        f'<defs><clipPath id="card-clip"><rect width="{CARD_WIDTH}" height="{CARD_HEIGHT}" rx="15"/></clipPath></defs>',
        '<style>text,tspan{white-space:pre}text{fill:' + FONT_COLOR + '}.key{fill:' + KEY_COLOR + '}.value{fill:' + VALUE_COLOR + '}.cc{fill:' + DOT_COLOR + '}.add{fill:' + SUCCESS_COLOR + '}.del{fill:' + ERROR_COLOR + '}.classified{fill:' + CLASSIFIED_COLOR + ';font-weight:700}</style>',
    ]
    if background_uri:
        out.append(
            f'<image href="{background_uri}" width="{CARD_WIDTH}" height="{CARD_HEIGHT}" '
            'preserveAspectRatio="xMidYMid slice" clip-path="url(#card-clip)"/>'
        )
    else:
        out.append(f'<rect width="{CARD_WIDTH}" height="{CARD_HEIGHT}" rx="15" fill="{BACKGROUND_FALLBACK_COLOR}"/>')
    out.extend([
        f'<rect width="{CARD_WIDTH}" height="{CARD_HEIGHT}" rx="15" fill="{BACKGROUND_OVERLAY_COLOR}" fill-opacity="{background_overlay:g}"/>',
        f'<rect x="1" y="1" width="{CARD_WIDTH - 2}" height="{CARD_HEIGHT - 2}" rx="14" fill="none" stroke="{VALUE_COLOR}" stroke-opacity=".18"/>',
        f'<g fill="none" stroke="{ORBIT_COLOR}" stroke-opacity="{orbit_opacity:g}"><ellipse cx="770" cy="245" rx="250" ry="106" transform="rotate(-18 770 245)"/><ellipse cx="770" cy="245" rx="190" ry="290" transform="rotate(32 770 245)"/><ellipse cx="770" cy="245" rx="315" ry="155" transform="rotate(14 770 245)"/><circle cx="770" cy="245" r="5" fill="{ORBIT_CENTER_COLOR}" fill-opacity="{orbit_center_opacity:g}" stroke="none"/><circle cx="531" cy="322" r="3" fill="{VALUE_COLOR}" stroke="none"/></g>',
        f'<svg x="{PORTRAIT_X}" y="{PORTRAIT_Y}" width="{portrait_width:g}" height="{portrait_height:g}" viewBox="0 0 {natural_width:g} {natural_height:g}" preserveAspectRatio="xMidYMid slice" overflow="hidden">',
        f'<text fill="{FONT_COLOR}" font-size="{ASCII_RENDER_FONT_SIZE:g}" xml:space="preserve">',
    ])
    for index, line in enumerate(portrait):
        row_y = ASCII_RENDER_FONT_SIZE + index * ASCII_RENDER_ROW_HEIGHT
        out.append(f'<tspan x="0" y="{row_y:g}">{html.escape(line)}</tspan>')
    out.append('</text></svg>')
    out.extend([
        text(30, terminal_name),
        text(30, " " * 25 + "-" + "—" * 42 + "-", "cc"),
        field_line(50, "Mission.Objective", config.get("mission_objective")),
        field_line(70, "Mission.Elapsed", uptime),
        field_line(90, "Organization", config.get("organization")),
        field_line(110, "Mission.Role", config.get("flight_kernel")),
        field_line(130, "Toolchain", config.get("toolchain")),
        field_line(150, "Languages.Human", config.get("languages_real")),
        text(180, "- Flight Systems " + "—" * 39),
        field_line(200, "Languages.Code", programming),
        field_line(220, "Domain", config.get("software_domain")),
        field_line(240, "Current.Build", config.get("current_build")),
        field_line(260, "Engineering.Values", config.get("engineering_values")),
        field_line(280, "Engineering.Mode", config.get("engineering_mode")),
        text(310, "- Mission Link " + "—" * 42),
        field_line(330, "Email.Personal", config.get("email_personal")),
        field_line(350, "Email.Work", config.get("email_work")),
        field_line(370, "LinkedIn", config.get("linkedin")),
        field_line(390, "GitHub", user.get("html_url")),
        text(430, "- Mission Telemetry " + "—" * 37),
        f'<text x="{RIGHT_X}" y="450"><tspan class="cc">. </tspan><tspan class="key">Commits.Indexed</tspan>: <tspan class="value">{commit_count:,}</tspan> | <tspan class="key">Repos.Contributed</tspan>: <tspan class="value">{contributed_count}</tspan></text>',
        f'<text x="{RIGHT_X}" y="470"><tspan class="cc">. </tspan><tspan class="key">Contributions.365d</tspan>: <tspan class="value">{annual_contributions:,}</tspan> | <tspan class="key">Private.Work</tspan>: <tspan class="classified">CLASSIFIED</tspan></text>',
        f'<text x="{RIGHT_X}" y="490"><tspan class="cc">. </tspan><tspan class="key">Automation</tspan>: <tspan class="add">NOMINAL</tspan><tspan class="cc"> · daily telemetry · </tspan><tspan class="value">{datetime.now(timezone.utc).strftime("%d %b %Y")}</tspan></text>',
    ])
    out.append('</svg>')
    return "\n".join(out) + "\n"


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    username = os.getenv("GITHUB_USERNAME") or config.get("handle")
    token = os.getenv("GITHUB_TOKEN", "")
    if not username:
        print("Set handle in profile.json or GITHUB_USERNAME", file=sys.stderr)
        return 2

    fallback_commits = int(config.get("commit_count_fallback", 0))
    contributed = int(config.get("contributed_repos", 0))
    annual_contributions = int(config.get("annual_contributions_fallback", 0))
    local_avatar = os.getenv("LOCAL_AVATAR") or config.get("local_avatar")
    running_in_actions = os.getenv("GITHUB_ACTIONS", "").lower() == "true"
    force_live_data = os.getenv("PROFILE_LIVE_DATA", "").lower() == "true"
    development_mode = bool(local_avatar) and not running_in_actions and not force_live_data

    if running_in_actions and not token:
        print("GITHUB_TOKEN is required when running in GitHub Actions", file=sys.stderr)
        return 2

    if development_mode:
        print("Development mode: skipping all GitHub API requests")
        user = {
            "login": username,
            "name": config.get("name") or username,
            "html_url": f"https://github.com/{username}",
            "created_at": config.get("birthday") or "2000-01-01T00:00:00Z",
        }
        repos: list[dict] = []
        languages: Counter = Counter()
        commits = fallback_commits
        try:
            avatar = load_avatar(config, user, token)
        except OSError as error:
            print(f"Local development avatar failed: {error}", file=sys.stderr)
            return 1
    else:
        try:
            user = api_get(f"/users/{username}", token)
            repos = api_get(f"/users/{username}/repos?per_page=100&type=owner", token)
            languages = fetch_languages(repos, token)
            avatar = load_avatar(config, user, token)
        except (OSError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
            print(f"GitHub request failed: {error}", file=sys.stderr)
            return 1
        try:
            live_commits = api_get(f"/search/commits?q=author:{username}", token).get(
                "total_count", 0
            )
            commits = max(fallback_commits, int(live_commits))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            commits = fallback_commits

    if token and not development_mode:
        try:
            now = datetime.now(timezone.utc)
            graph_data = api_graphql(
                "query($login:String!,$from:DateTime!,$to:DateTime!){user(login:$login){repositoriesContributedTo(first:1,contributionTypes:[COMMIT,ISSUE,PULL_REQUEST,REPOSITORY]){totalCount} contributionsCollection(from:$from,to:$to){contributionCalendar{totalContributions}}}}",
                {
                    "login": username,
                    "from": (now - timedelta(days=365)).isoformat(),
                    "to": now.isoformat(),
                },
                token,
            )["user"]
            contributed = max(
                contributed,
                int(graph_data["repositoriesContributedTo"]["totalCount"]),
            )
            annual_contributions = max(
                annual_contributions,
                int(
                    graph_data["contributionsCollection"]["contributionCalendar"][
                        "totalContributions"
                    ]
                ),
            )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError):
            pass
    svg = render_svg(
        config,
        user,
        repos,
        languages,
        ascii_portrait(avatar, config.get("ascii", {})),
        commits,
        contributed,
        annual_contributions,
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(svg, encoding="utf-8", newline="\n")
    cache_key = update_readme_cache_key(svg)
    print(
        f"Generated {OUTPUT_PATH.relative_to(ROOT)} for @{username} "
        f"(cache key: {cache_key})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
