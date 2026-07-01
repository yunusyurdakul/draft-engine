<p align="center">
  <strong>⚔️ Draft Engine</strong>
</p>

<p align="center">
  <em>Real-time League of Legends AI drafting assistant powered by Gemini</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/streamlit-1.35%2B-FF4B4B?style=flat-square&logo=streamlit&logoColor=white" alt="Streamlit">
  <img src="https://img.shields.io/badge/gemini-2.5--flash-4285F4?style=flat-square&logo=google&logoColor=white" alt="Gemini">
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License">
</p>

---

## 🏠 Overview

**Draft Engine** is a Streamlit-powered desktop tool that connects directly to the **League of Legends client (LCU API)** during champion select and provides AI-driven drafting recommendations via **Google Gemini**. It monitors your draft in real time — picks, bans, and team compositions — then delivers role-specific champion suggestions complete with rune setups, item builds, and matchup breakdowns.

### ✨ Key Features

| Feature | Description |
|---|---|
| 🔌 **LCU Auto-Connect** | Automatically detects the League client via its lockfile and establishes an authenticated connection — no manual setup required. |
| 📡 **Real-Time Draft Monitoring** | Polls champion select every 2 seconds, displaying live hover states, locked picks, and bans on an interactive draft board. |
| 🤖 **Gemini AI Recommendations** | Generates a detailed champion recommendation only when the draft state changes (a pick is locked or a ban is completed), minimising API usage. |
| 🎯 **Role-Aware Analysis** | Recommendations factor in your selected role, team comp synergy, damage profile balance, lane matchups, and enemy counters. |
| ⚔️ **Loadout Guidance** | Each recommendation includes keystone rune selection and a core 3-item build path with tactical reasoning. |
| 🖼️ **Champion Portraits** | Fetches champion icons from Riot's Data Dragon CDN for a polished, visual draft board. |
| 🎨 **Hextech Dark Theme** | Custom-styled UI with League-inspired gold accents, animated live indicators, and glassmorphism cards. |

---

## 📋 Prerequisites

- **Python 3.10+**
- **League of Legends** installed on Windows (default lockfile path: `C:\Riot Games\League of Legends\lockfile`)
- A **Google Gemini API key** — [get one here](https://aistudio.google.com/apikey)

---

## 🚀 Getting Started

### 1. Clone the Repository

```bash
git clone https://github.com/your-username/draft-engine.git
cd draft-engine
```

### 2. Create a Virtual Environment

```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Your API Key

Copy the environment template and add your Gemini API key:

```bash
copy .env.template .env
```

Then edit `.env`:

```
GEMINI_API_KEY=your_api_key_here
```

Alternatively, you can enter the key directly in the app's sidebar at runtime.

### 5. Launch the App

```bash
streamlit run app.py
```

The app will open in your browser at `http://localhost:8501`.

---

## 🎮 Usage

1. **Launch the League client** and enter a matchmade queue.
2. **Start Draft Engine** — it will automatically detect the client and wait for champion select.
3. **Select your role** in the sidebar (Top, Jungle, Mid, ADC, or Support).
4. Once champion select begins, the **draft board** populates in real time with picks and bans.
5. **AI recommendations** appear automatically each time the draft state changes — a champion is locked in or a ban is completed.

### App States

| State | What You'll See |
|---|---|
| 🔌 Client not detected | A message asking you to launch the League client. |
| ⏳ Waiting for draft | The client is running but you're not in champion select yet. |
| 🟢 LIVE — Champion Select | Full draft board with picks, bans, and AI recommendations. |

---

## 🏗️ Architecture

```
draft-engine/
├── app.py                  # Main application (single-file architecture)
├── requirements.txt        # Python dependencies
├── .env.template           # Environment variable template
├── .gitignore
└── .streamlit/
    └── config.toml         # Streamlit theme & server configuration
```

### Module Breakdown (`app.py`)

The application is organized into six clearly delineated sections:

| Section | Responsibility |
|---|---|
| **1. Data Dragon** | Fetches and caches champion ID → name mappings from Riot's CDN. |
| **2. LCU API** | Reads the client lockfile and polls the champion-select session endpoint. |
| **3. Draft State** | Extracts a simplified draft state (picks, bans, teams) from raw session JSON. |
| **4. Gemini Inference** | Constructs a structured prompt and calls the Gemini API for recommendations. |
| **5. UI Helpers** | Renders champion portraits, team columns, and ban bars with Data Dragon icons. |
| **6. Main App** | Orchestrates the Streamlit layout, polling loop, and recommendation caching. |

### Data Flow

```
League Client (LCU API)
        │
        ▼
   Read Lockfile ──► Authenticate ──► Poll /lol-champ-select/v1/session
        │
        ▼
  Extract Draft State ──► Compute Signature
        │                        │
        ▼                        ▼
  Render Draft Board      Changed? ──► Gemini API ──► Recommendation Card
                              │
                              ▼
                        Cache & Display
```

---

## ⚙️ Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Your Google Gemini API key. Can also be entered via the sidebar. |

### Streamlit Theme (`.streamlit/config.toml`)

The app ships with a custom Hextech-inspired dark theme:

| Setting | Value | Purpose |
|---|---|---|
| `primaryColor` | `#C89B3C` | League gold accent |
| `backgroundColor` | `#010A13` | Deep dark background |
| `secondaryBackgroundColor` | `#0A1428` | Card / sidebar background |
| `textColor` | `#A09B8C` | Muted parchment text |

### Constants (in `app.py`)

| Constant | Default | Description |
|---|---|---|
| `LOCKFILE_PATH` | `C:\Riot Games\League of Legends\lockfile` | Path to the League client lockfile. Adjust if your install location differs. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | The Gemini model used for recommendations. |
| `POLL_INTERVAL_SECONDS` | `2` | How frequently (in seconds) the app polls the LCU for draft updates. |

---

## 🛠️ Tech Stack

| Technology | Role |
|---|---|
| [Streamlit](https://streamlit.io/) | Web UI framework |
| [Google Gemini](https://ai.google.dev/) | AI inference for draft recommendations |
| [Riot LCU API](https://developer.riotgames.com/) | Live champion-select data |
| [Data Dragon](https://developer.riotgames.com/docs/lol#data-dragon) | Champion metadata and icon assets |
| [python-dotenv](https://pypi.org/project/python-dotenv/) | Environment variable management |

---

## 🤝 Contributing

Contributions are welcome! To get started:

1. **Fork** the repository
2. **Create** a feature branch (`git checkout -b feature/my-feature`)
3. **Commit** your changes (`git commit -m "Add my feature"`)
4. **Push** to the branch (`git push origin feature/my-feature`)
5. **Open** a Pull Request

---

## 📜 License

This project is provided under the [MIT License](LICENSE).

---

## ⚠️ Disclaimer

**Draft Engine** is a fan-made tool and is **not** endorsed by or affiliated with Riot Games. *League of Legends* and all related assets are trademarks of Riot Games, Inc. Use this tool responsibly and in accordance with Riot's Terms of Service.
