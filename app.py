"""
Antigravity Draft Engine — League of Legends AI Drafting Assistant
==================================================================
A Streamlit application that monitors the League Client (LCU API) in
real-time and uses Gemini to produce role-aware champion / rune / item
recommendations during champion select.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import requests
import streamlit as st
import urllib3
from google import genai

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOCKFILE_PATH = Path(r"C:\Riot Games\League of Legends\lockfile")
LCU_SESSION_ENDPOINT = "/lol-champ-select/v1/session"
DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_CHAMPIONS_URL = (
    "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
)
ROLES = ["Top", "Jungle", "Mid", "ADC", "Support"]
GEMINI_MODEL = "gemini-2.5-flash"
POLL_INTERVAL_SECONDS = 2

# Suppress the InsecureRequestWarning emitted by urllib3 when we hit the
# LCU's self-signed TLS certificate.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# 1. Data Dragon — champion ID ➜ name mapping
# ---------------------------------------------------------------------------


@st.cache_data(show_spinner="Fetching champion data from Data Dragon …")
def fetch_champion_map() -> dict[int, str]:
    """Return a dict mapping numeric champion IDs to display names.

    ID ``0`` is mapped to ``"None"`` to represent an empty slot.
    """
    versions: list[str] = requests.get(DDRAGON_VERSIONS_URL, timeout=10).json()
    latest_version = versions[0]

    champions_data: dict[str, Any] = requests.get(
        DDRAGON_CHAMPIONS_URL.format(version=latest_version), timeout=10
    ).json()

    mapping: dict[int, str] = {0: "None"}
    for champ_info in champions_data["data"].values():
        mapping[int(champ_info["key"])] = champ_info["name"]

    return mapping


# ---------------------------------------------------------------------------
# 2. LCU API — locate lockfile & poll champion-select session
# ---------------------------------------------------------------------------


def read_lockfile() -> tuple[str, int, str] | None:
    """Parse the League client lockfile.

    Returns ``(protocol, port, password)`` or ``None`` when the file is
    missing (client not running).
    """
    if not LOCKFILE_PATH.exists():
        return None

    try:
        text = LOCKFILE_PATH.read_text(encoding="utf-8").strip()
        parts = text.split(":")
        # Format: process_name:pid:port:password:protocol
        if len(parts) < 5:
            return None
        _name, _pid, port_str, password, protocol = parts[:5]
        return protocol, int(port_str), password
    except Exception:
        return None


def fetch_champ_select_session(
    protocol: str, port: int, password: str
) -> dict[str, Any] | None:
    """GET the current champion-select session from the LCU.

    Returns the parsed JSON on success, or ``None`` if the client is not
    in an active champion-select phase.
    """
    url = f"{protocol}://127.0.0.1:{port}{LCU_SESSION_ENDPOINT}"
    try:
        resp = requests.get(
            url,
            auth=("riot", password),
            verify=False,  # self-signed cert
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except requests.ConnectionError:
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 3. Draft state extraction helpers
# ---------------------------------------------------------------------------


def _completed_actions(
    actions_list: list[list[dict]], action_type: str, *, ally_only: bool | None = None
) -> list[int]:
    """Flatten nested action groups and collect completed champion IDs.

    Parameters
    ----------
    ally_only
        If ``True`` only ally actions, if ``False`` only enemy actions,
        if ``None`` all actions regardless of side.
    """
    ids: list[int] = []
    for group in actions_list:
        for action in group:
            if action.get("type") != action_type or not action.get("completed"):
                continue
            if ally_only is True and not action.get("isAllyAction"):
                continue
            if ally_only is False and action.get("isAllyAction"):
                continue
            cid = action.get("championId", 0)
            ids.append(cid)
    return ids


def _is_local_player_picking(session: dict[str, Any]) -> bool:
    """Return True when the local player has an active (uncompleted) pick action."""
    local_cell = session.get("localPlayerCellId", -1)
    for group in session.get("actions", []):
        for action in group:
            if (
                action.get("actorCellId") == local_cell
                and action.get("type") == "pick"
                and action.get("isInProgress", False)
            ):
                return True
    return False


def _local_player_locked_champion(
    session: dict[str, Any],
) -> int | None:
    """Return the champion ID the local player locked in, or ``None``."""
    local_cell = session.get("localPlayerCellId", -1)
    for group in session.get("actions", []):
        for action in group:
            if (
                action.get("actorCellId") == local_cell
                and action.get("type") == "pick"
                and action.get("completed", False)
            ):
                cid = action.get("championId", 0)
                if cid != 0:
                    return cid
    return None


def _is_my_pick_next(session: dict[str, Any]) -> bool:
    """Return True when the action group immediately before the player's pick is in progress.

    This lets us pre-generate the recommendation so it's cached and
    displayed instantly when the player's turn actually starts.
    """
    local_cell = session.get("localPlayerCellId", -1)
    actions = session.get("actions", [])

    # Find the group index containing the player's first uncompleted pick.
    my_pick_group_idx: int | None = None
    for group_idx, group in enumerate(actions):
        for action in group:
            if (
                action.get("actorCellId") == local_cell
                and action.get("type") == "pick"
                and not action.get("completed", False)
            ):
                my_pick_group_idx = group_idx
                break
        if my_pick_group_idx is not None:
            break

    if my_pick_group_idx is None or my_pick_group_idx == 0:
        return False

    # Check if the immediately preceding group has an in-progress action.
    prev_group = actions[my_pick_group_idx - 1]
    return any(action.get("isInProgress", False) for action in prev_group)


def extract_draft_state(
    session: dict[str, Any],
) -> dict[str, Any]:
    """Derive a simplified draft state from the raw session JSON.

    Returns a dict with keys:
      - ``blue_picks``:    live hover/selection IDs for UI display
      - ``red_picks``:     live hover/selection IDs for UI display
      - ``locked_blue``:   only locked-in (completed) ally picks
      - ``locked_red``:    only locked-in (completed) enemy picks
      - ``bans``:          completed bans
      - ``local_team``:    ``"blue"`` or ``"red"``
      - ``is_my_pick_turn``: ``True`` when the local player is actively picking
      - ``is_my_pick_next``: ``True`` when the turn before the player's pick is active
      - ``my_champion``:  locked champion ID (int) or ``None``
    """
    my_team: list[dict] = session.get("myTeam", [])
    their_team: list[dict] = session.get("theirTeam", [])
    actions: list[list[dict]] = session.get("actions", [])

    # Live hover state — used for the draft board UI only.
    blue_picks = [m.get("championId", 0) for m in my_team]
    red_picks = [m.get("championId", 0) for m in their_team]

    # Locked-in state — used for Gemini trigger & prompt.
    locked_blue = _completed_actions(actions, "pick", ally_only=True)
    locked_red = _completed_actions(actions, "pick", ally_only=False)
    bans = _completed_actions(actions, "ban")

    local_team = "blue"

    return {
        "blue_picks": blue_picks,
        "red_picks": red_picks,
        "locked_blue": locked_blue,
        "locked_red": locked_red,
        "bans": bans,
        "local_team": local_team,
        "is_my_pick_turn": _is_local_player_picking(session),
        "is_my_pick_next": _is_my_pick_next(session),
        "my_champion": _local_player_locked_champion(session),
    }


def draft_signature(state: dict[str, Any]) -> str:
    """Produce a deterministic string from *locked-in* picks and bans.

    The Gemini API will only be called when this signature differs from
    the previously recorded one — i.e. when a champion is actually locked
    in or a ban is completed.
    """
    return (
        f"B:{sorted(state['locked_blue'])}"
        f"|R:{sorted(state['locked_red'])}"
        f"|X:{sorted(state['bans'])}"
    )


# ---------------------------------------------------------------------------
# 4. Gemini inference
# ---------------------------------------------------------------------------


def build_prompt(
    role: str,
    ally_picks: list[str],
    enemy_picks: list[str],
    bans: list[str],
    patch_version: str,
) -> str:
    """Construct a structured prompt for the Gemini model."""
    allies = ", ".join(ally_picks) if ally_picks else "None yet"
    enemies = ", ".join(enemy_picks) if enemy_picks else "None yet"
    bans_str = ", ".join(bans) if bans else "None yet"

    return (
        "You are an elite, high-ELO League of Legends analyst, draft coach, "
        "and statistical engine. Your sole objective is to provide a single, "
        "flawless, highly optimized champion recommendation for the user's "
        "selected role, based strictly on the current live draft state.\n\n"
        f"### CURRENT PATCH: {patch_version}\n"
        "All of your recommendations MUST reflect the **latest balance changes, "
        "item reworks, rune adjustments, and meta shifts** introduced in this "
        "patch. Consider:\n"
        "- Recent champion buffs/nerfs that affect tier-list placement.\n"
        "- Any new, reworked, or removed items that change optimal build paths.\n"
        "- Rune keystones or minor runes that were buffed, nerfed, or reworked.\n"
        "- Emerging meta trends and high win-rate strategies on this patch.\n"
        "Do NOT recommend builds, runes, or champion picks based on outdated "
        "patch information.\n\n"
        "### INPUT DRAFT STATE:\n"
        f"- Player Role: {role}\n"
        f"- Ally Team Picks: {allies}\n"
        f"- Enemy Team Picks: {enemies}\n"
        f"- Banned Champions: {bans_str}\n\n"
        "### ANALYSIS PARADIGM:\n"
        "Evaluate the draft using the following hierarchical criteria:\n"
        "1. ELIGIBILITY: You MUST NOT recommend any champion that is currently "
        "banned, already picked by either team, or completely non-viable in the "
        "designated [Player Role].\n"
        "2. DAMAGE PROFILE BALANCE: Analyze the Ally Team's damage distribution. "
        "If the team is heavily skewed toward one damage type (e.g., full AD or "
        "full AP), prioritize a champion that balances the profile unless the "
        "enemy team cannot itemize against it.\n"
        "3. LANING PHASE & COUNTERPICKING: Analyze the direct lane matchup if "
        "the enemy laner is revealed. Assess wave control, kill pressure, and "
        "jungle setup capabilities.\n"
        "4. TEAM COMPOSITIONAL SYNERGY: Identify the Ally Team's identity "
        "(e.g., Poke/Disengage, Hard Engage, Dive, Split-push, Front-to-Back). "
        "Recommend a champion that amplifies this identity or patches a critical "
        "vulnerability (e.g., adding frontline/peel if the team is entirely "
        "squishy carries).\n"
        "5. ENEMY TEAM COUNTERING: Identify the core threat or win condition of "
        "the Enemy Team (e.g., heavy crowd control, hyper-carries, high mobility, "
        "triple tank). Choose a champion whose kit mechanically neutralizes their "
        "primary win condition.\n\n"
        "### OUTPUT SPECIFICATION:\n"
        "Provide your response in a clean, highly readable Markdown format. "
        "Do not include lengthy introductions. Go straight into the analysis "
        "using this template:\n\n"
        "## 🏆 Recommended Pick: **[CHAMPION NAME]**\n\n"
        "### 🧠 Draft Rationale\n"
        "* **Direct Matchup:** [Explain how this pick wins or neutralizes the "
        "direct lane opponent, or secures priority if blind picking].\n"
        "* **Team Synergy:** [Explain how this champion's kit synergizes with "
        "the Ally Team's compositions and win condition].\n"
        "* **Enemy Countering:** [Explain which specific enemy champions, "
        "mechanics, or compositions this pick hard-counters in teamfights].\n\n"
        "### ⚔️ Loadout Setup\n"
        "* **Keystone Rune:** [Exact Name] -> [Brief 1-sentence reason why "
        "this specific keystone is chosen for this matchup].\n"
        "* **Core Build Path:** [Item 1], [Item 2], [Item 3] -> [Briefly "
        "state the tactical purpose of this item spike against the enemy "
        "team composition].\n\n"
        "### 🎯 Enemy Matchup Breakdown\n"
        "For **each** revealed enemy champion, provide a bullet explaining "
        "why your recommended pick is strong against them. Use this format:\n"
        "* **[Enemy Champion Name]:** [1-2 sentences on why your pick wins "
        "or neutralizes this enemy — reference specific abilities, stat "
        "checks, range advantages, or kit interactions].\n"
    )


def get_gemini_recommendation(
    role: str,
    ally_names: list[str],
    enemy_names: list[str],
    ban_names: list[str],
    api_key: str,
    patch_version: str,
) -> str:
    """Call Gemini and return the recommendation text."""
    client = genai.Client(api_key=api_key)
    prompt = build_prompt(role, ally_names, enemy_names, ban_names, patch_version)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    return response.text or "_No response received._"


def build_post_pick_prompt(
    champion: str,
    role: str,
    ally_picks: list[str],
    enemy_picks: list[str],
    patch_version: str,
) -> str:
    """Construct a prompt that analyses the player's locked champion strengths."""
    allies = ", ".join(ally_picks) if ally_picks else "None yet"
    enemies = ", ".join(enemy_picks) if enemy_picks else "None yet"

    return (
        "You are an elite, high-ELO League of Legends analyst and in-game "
        "coach. The player has just locked in their champion. Your job is to "
        "provide a comprehensive strengths and game-plan briefing so they "
        "know exactly how to win with their pick.\n\n"
        f"### CURRENT PATCH: {patch_version}\n"
        "All advice MUST reflect the latest balance changes, item reworks, "
        "rune adjustments, and meta shifts on this patch.\n\n"
        "### LOCKED PICK:\n"
        f"- Champion: **{champion}**\n"
        f"- Role: {role}\n"
        f"- Ally Team: {allies}\n"
        f"- Enemy Team: {enemies}\n\n"
        "### ANALYSIS:\n"
        "Provide your response in clean, highly readable Markdown. "
        "Go straight into the analysis using this template:\n\n"
        f"## 🛡️ Game Plan: **{champion}**\n\n"
        "### 💪 Your Strong Sides\n"
        "List 3-5 key strengths of this champion in this specific matchup "
        "and team composition. Reference abilities, stats, and kit "
        "interactions that give you an edge.\n\n"
        "### ⚡ Power Spikes\n"
        "* **Early (Lv 1-6):** [Describe your early game win condition — "
        "when to trade, all-in, or farm safely].\n"
        "* **Mid (1-2 items):** [Describe what changes when you hit your "
        "first major item spike and how to leverage it].\n"
        "* **Late (3+ items):** [Describe your late-game role and how "
        "strong you scale relative to the enemy].\n\n"
        "### 🎯 Matchup Tips vs Enemy Team\n"
        "For **each** revealed enemy champion, provide a bullet with "
        "actionable advice:\n"
        "* **[Enemy Champion]:** [How to play against them — abilities "
        "to dodge, when you win trades, and what to avoid].\n\n"
        "### 🗺️ Win Condition\n"
        "Summarize in 2-3 sentences exactly how this team wins the game "
        "with your champion. Focus on macro objectives, teamfight role, "
        "and split-push vs. group decision.\n"
    )


def get_gemini_post_pick(
    champion: str,
    role: str,
    ally_names: list[str],
    enemy_names: list[str],
    api_key: str,
    patch_version: str,
) -> str:
    """Call Gemini with the post-pick strengths prompt."""
    client = genai.Client(api_key=api_key)
    prompt = build_post_pick_prompt(
        champion, role, ally_names, enemy_names, patch_version
    )
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    return response.text or "_No response received._"


# ---------------------------------------------------------------------------
# 5. UI helpers
# ---------------------------------------------------------------------------

# Champion square URLs from Data Dragon (used for portraits).
_DDRAGON_SQUARE_URL = (
    "https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{champion_key}.png"
)


@st.cache_data(show_spinner=False)
def _latest_ddragon_version() -> str:
    versions: list[str] = requests.get(DDRAGON_VERSIONS_URL, timeout=10).json()
    return versions[0]


@st.cache_data(show_spinner=False)
def _champion_key_map() -> dict[str, str]:
    """Return champion *name* → Data Dragon *key* (file-safe id string)."""
    version = _latest_ddragon_version()
    data = requests.get(
        DDRAGON_CHAMPIONS_URL.format(version=version), timeout=10
    ).json()
    return {info["name"]: key for key, info in data["data"].items()}


def _champion_icon_url(name: str) -> str:
    """Build the Data Dragon icon URL for *name*."""
    key_map = _champion_key_map()
    key = key_map.get(name, name)
    version = _latest_ddragon_version()
    return _DDRAGON_SQUARE_URL.format(version=version, champion_key=key)


def render_team_column(
    title: str,
    pick_ids: list[int],
    champ_map: dict[int, str],
    accent: str,
) -> None:
    """Render a single team column with champion portraits."""
    st.markdown(
        f"<h3 style='text-align:center;color:{accent};'>{title}</h3>",
        unsafe_allow_html=True,
    )
    for cid in pick_ids:
        name = champ_map.get(cid, "Unknown")
        if name == "None" or cid == 0:
            st.markdown(
                "<div style='"
                "height:56px;width:56px;margin:6px auto;"
                "border:2px dashed #1E2328;border-radius:8px;"
                "display:flex;align-items:center;justify-content:center;"
                "color:#3C3C41;font-size:22px;"
                "'>?</div>",
                unsafe_allow_html=True,
            )
        else:
            icon = _champion_icon_url(name)
            st.markdown(
                f"<div style='text-align:center;margin:6px 0;'>"
                f"<img src='{icon}' width='56' "
                f"style='border:2px solid {accent};border-radius:8px;'>"
                f"<div style='color:#A09B8C;font-size:13px;'>{name}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )


def render_bans_bar(ban_ids: list[int], champ_map: dict[int, str]) -> None:
    """Render a horizontal bar of banned champions."""
    if not ban_ids:
        st.caption("No bans yet.")
        return
    ban_names = [champ_map.get(cid, "Unknown") for cid in ban_ids if cid != 0]
    if not ban_names:
        st.caption("No bans yet.")
        return
    icons_html = ""
    for name in ban_names:
        icon = _champion_icon_url(name)
        icons_html += (
            f"<div style='display:inline-block;margin:0 4px;text-align:center;'>"
            f"<img src='{icon}' width='40' "
            f"style='border:2px solid #C8414B;border-radius:6px;opacity:0.55;filter:grayscale(60%);'>"
            f"<div style='font-size:11px;color:#C8414B;'>{name}</div>"
            f"</div>"
        )
    st.markdown(
        f"<div style='text-align:center;padding:6px 0;'>{icons_html}</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# 6. Main application
# ---------------------------------------------------------------------------

CSS = """
<style>
/* Global overrides for a polished dark-Hextech look */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
}

/* Gold glow on headers */
h1, h2, h3 {
    font-weight: 700 !important;
    letter-spacing: 0.5px;
}
h1 {
    background: linear-gradient(135deg, #C89B3C 0%, #F0E6D2 50%, #C89B3C 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    text-align: center;
}

/* Subtle gold divider */
hr {
    border: none;
    height: 1px;
    background: linear-gradient(90deg, transparent, #785A28, transparent);
}

/* Cards */
div[data-testid="stExpander"] {
    border: 1px solid #1E2328 !important;
    border-radius: 10px;
    background: #0A1428 !important;
}

/* Sidebar polish */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #010A13 0%, #0A1428 100%) !important;
    border-right: 1px solid #1E2328;
}

/* Animated pulse for the live indicator */
@keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(0, 200, 83, 0.5); }
    70%  { box-shadow: 0 0 0 8px rgba(0, 200, 83, 0); }
    100% { box-shadow: 0 0 0 0 rgba(0, 200, 83, 0); }
}
.live-dot {
    display: inline-block;
    width: 10px; height: 10px;
    background: #00C853;
    border-radius: 50%;
    margin-right: 6px;
    animation: pulse 1.5s infinite;
}

/* Recommendation card */
.reco-card {
    background: linear-gradient(145deg, #0A1428, #091428);
    border: 1px solid #785A28;
    border-radius: 12px;
    padding: 20px 24px;
    margin-top: 12px;
    box-shadow: 0 4px 24px rgba(200, 155, 60, 0.08);
}

/* Status banners */
.status-banner {
    text-align: center;
    padding: 40px 20px;
    color: #5B5A56;
    font-size: 15px;
}
.status-banner .icon { font-size: 48px; margin-bottom: 12px; }

/* YOUR TURN banner */
@keyframes turn-glow {
    0%   { box-shadow: 0 0 8px rgba(200, 155, 60, 0.3); }
    50%  { box-shadow: 0 0 20px rgba(200, 155, 60, 0.6); }
    100% { box-shadow: 0 0 8px rgba(200, 155, 60, 0.3); }
}
.your-turn-banner {
    text-align: center;
    padding: 14px 20px;
    margin: 8px 0 12px 0;
    background: linear-gradient(135deg, #1a1203 0%, #2a1f08 50%, #1a1203 100%);
    border: 1px solid #C89B3C;
    border-radius: 10px;
    color: #F0E6D2;
    font-weight: 700;
    font-size: 16px;
    letter-spacing: 1px;
    animation: turn-glow 2s ease-in-out infinite;
}
</style>
"""


def main() -> None:
    st.set_page_config(
        page_title="Draft Engine — LoL AI Assistant",
        page_icon="⚔️",
        layout="wide",
    )
    st.markdown(CSS, unsafe_allow_html=True)

    # ── Sidebar ──────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("## ⚙️ Settings")
        role = st.selectbox("Your Role", ROLES, index=2)

        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            api_key = st.text_input(
                "Gemini API Key",
                type="password",
                help="Provide your Gemini API key or set the GEMINI_API_KEY env var.",
            )
        else:
            st.success("API key loaded from environment.")

        st.divider()
        st.caption(
            "Draft Engine polls the League client every 2 s.  "
            "Recommendations update automatically when the draft changes."
        )

    # ── Title ────────────────────────────────────────────────────────────
    st.markdown("# ⚔️ Draft Engine")
    st.markdown(
        "<p style='text-align:center;color:#5B5A56;margin-top:-10px;'>"
        "Real-time AI drafting assistant powered by Gemini</p>",
        unsafe_allow_html=True,
    )
    st.markdown("---")

    # ── Load champion map ────────────────────────────────────────────────
    champ_map = fetch_champion_map()

    # ── Attempt to connect to the LCU ────────────────────────────────────
    lockfile_data = read_lockfile()
    if lockfile_data is None:
        st.markdown(
            "<div class='status-banner'>"
            "<div class='icon'>🔌</div>"
            "League client not detected.<br>"
            "Launch the client and enter champion select to begin."
            "</div>",
            unsafe_allow_html=True,
        )
        time.sleep(POLL_INTERVAL_SECONDS)
        st.rerun()
        return

    protocol, port, password = lockfile_data
    session = fetch_champ_select_session(protocol, port, password)

    if session is None:
        st.markdown(
            "<div class='status-banner'>"
            "<div class='icon'>⏳</div>"
            "Waiting for champion select…<br>"
            "The draft board will appear automatically once a lobby starts."
            "</div>",
            unsafe_allow_html=True,
        )
        # Reset stored state so fresh recommendations fire next time.
        for _key in ("last_sig", "last_reco", "pick_reco", "post_pick_reco"):
            st.session_state.pop(_key, None)
        time.sleep(POLL_INTERVAL_SECONDS)
        st.rerun()
        return

    # ── We are in champion select ────────────────────────────────────────
    st.markdown(
        "<span class='live-dot'></span>"
        "<span style='color:#00C853;font-weight:600;font-size:14px;'>"
        "LIVE — Champion Select</span>",
        unsafe_allow_html=True,
    )

    draft = extract_draft_state(session)

    # -- Draft board ---
    st.markdown("### 🗡️ Draft Board")
    col_blue, col_vs, col_red = st.columns([2, 1, 2])
    with col_blue:
        render_team_column("🔵 Blue Side (Allies)", draft["blue_picks"], champ_map, "#0397AB")
    with col_vs:
        st.markdown(
            "<div style='display:flex;align-items:center;justify-content:center;"
            "height:100%;'>"
            "<span style='font-size:36px;color:#1E2328;font-weight:700;'>VS</span>"
            "</div>",
            unsafe_allow_html=True,
        )
    with col_red:
        render_team_column("🔴 Red Side (Enemies)", draft["red_picks"], champ_map, "#C8414B")

    # -- Bans ---
    with st.expander("🚫 Bans", expanded=True):
        render_bans_bar(draft["bans"], champ_map)

    st.markdown("---")

    # ── Turn / phase detection ────────────────────────────────────────────
    is_my_turn = draft["is_my_pick_turn"]
    my_champion_id = draft["my_champion"]
    my_champion_name = (
        champ_map.get(my_champion_id, "Unknown") if my_champion_id else None
    )

    # Shared name lists used by both prompts.
    ally_names = [
        champ_map.get(cid, "Unknown")
        for cid in draft["locked_blue"]
        if cid != 0
    ]
    enemy_names = [
        champ_map.get(cid, "Unknown")
        for cid in draft["locked_red"]
        if cid != 0
    ]
    ban_names = [
        champ_map.get(cid, "Unknown")
        for cid in draft["bans"]
        if cid != 0
    ]

    if not api_key:
        st.warning("Enter your Gemini API key in the sidebar to receive recommendations.")

    # ── PHASE 2: Player has locked in → strengths analysis ────────────────
    elif my_champion_name:
        # Show locked-in banner.
        icon = _champion_icon_url(my_champion_name)
        st.markdown(
            f"<div class='your-turn-banner' style='border-color:#0397AB;'>"
            f"<img src='{icon}' width='32' "
            f"style='vertical-align:middle;border-radius:6px;margin-right:8px;'>"
            f"LOCKED IN — {my_champion_name}"
            f"</div>",
            unsafe_allow_html=True,
        )

        # Generate post-pick analysis exactly once per game.
        if "post_pick_reco" not in st.session_state:
            with st.spinner("🤖 Gemini is analyzing your strengths…"):
                try:
                    analysis = get_gemini_post_pick(
                        my_champion_name, role,
                        ally_names, enemy_names, api_key,
                        patch_version=_latest_ddragon_version(),
                    )
                    st.session_state["post_pick_reco"] = analysis
                except Exception as exc:
                    st.error(f"Gemini API error: {exc}")

        cached_post = st.session_state.get("post_pick_reco")
        if cached_post:
            st.markdown("### 💪 Your Champion Strengths")
            st.markdown(
                f"<div class='reco-card'>{_md_to_html_safe(cached_post)}</div>",
                unsafe_allow_html=True,
            )

    # ── PHASE 1: It's our pick turn → champion recommendation (once) ─────
    elif is_my_turn:
        st.markdown(
            "<div class='your-turn-banner'>🎯 YOUR TURN — Pick a champion!</div>",
            unsafe_allow_html=True,
        )

        # Generate pick recommendation exactly once per pick turn.
        if "pick_reco" not in st.session_state:
            with st.spinner("🤖 Gemini is analyzing the draft…"):
                try:
                    recommendation = get_gemini_recommendation(
                        role, ally_names, enemy_names, ban_names, api_key,
                        patch_version=_latest_ddragon_version(),
                    )
                    st.session_state["pick_reco"] = recommendation
                except Exception as exc:
                    st.error(f"Gemini API error: {exc}")

        cached_pick = st.session_state.get("pick_reco")
        if cached_pick:
            st.markdown("### 🤖 AI Recommendation")
            st.markdown(
                f"<div class='reco-card'>{_md_to_html_safe(cached_pick)}</div>",
                unsafe_allow_html=True,
            )

    # ── Waiting phase (bans / enemy turns) ────────────────────────────────
    else:
        # Pre-generate recommendation when the turn before ours is active.
        if (
            api_key
            and draft["is_my_pick_next"]
            and "pick_reco" not in st.session_state
        ):
            with st.spinner("🤖 Pre-loading recommendation for your turn…"):
                try:
                    recommendation = get_gemini_recommendation(
                        role, ally_names, enemy_names, ban_names, api_key,
                        patch_version=_latest_ddragon_version(),
                    )
                    st.session_state["pick_reco"] = recommendation
                except Exception as exc:
                    st.error(f"Gemini API error: {exc}")

        cached_pick = st.session_state.get("pick_reco")
        if cached_pick:
            st.markdown("### 🤖 AI Recommendation")
            st.markdown(
                f"<div class='reco-card'>{_md_to_html_safe(cached_pick)}</div>",
                unsafe_allow_html=True,
            )
        else:
            st.info("⏳ Waiting for your pick turn…")

    # ── Auto-refresh ─────────────────────────────────────────────────────
    time.sleep(POLL_INTERVAL_SECONDS)
    st.rerun()


def _md_to_html_safe(md_text: str) -> str:
    """Minimal Markdown → HTML conversion for the recommendation card.

    We intentionally keep this simple — Streamlit's ``st.markdown`` already
    handles most rendering; we just need basic formatting inside our
    custom ``<div>``.
    """
    import re

    html = md_text
    # Bold
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    # Italic
    html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html)
    # Numbered list items
    html = re.sub(r"^(\d+)\.\s", r"<br><strong>\1.</strong> ", html, flags=re.MULTILINE)
    # Line breaks
    html = html.replace("\n", "<br>")
    return html


if __name__ == "__main__":
    main()
