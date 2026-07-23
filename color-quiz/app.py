from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from itertools import permutations
import sqlite3
import json
import random
from datetime import datetime
from pathlib import Path

app = FastAPI()

DB_PATH     = Path(__file__).parent / "results.db"
STATIC_PATH = Path(__file__).parent / "static"

# ── Edit this list to change participants ────────────────────────────────────
PARTICIPANTS = ["Chris", "Caio", "Zoe", "Justin", "Sean", "Grant", "Christian", "Craig"]

# ── Word list — 8 words × 10 colors = 80 words ──────────────────────────────
WORDS = [
    # Red — The Driver
    {"word": "Bold",          "color": "red"},
    {"word": "Decisive",      "color": "red"},
    {"word": "Competitive",   "color": "red"},
    {"word": "Assertive",     "color": "red"},
    {"word": "Driven",        "color": "red"},
    {"word": "Ambitious",     "color": "red"},
    {"word": "Commanding",    "color": "red"},
    {"word": "Fearless",      "color": "red"},
    # Orange — The Connector
    {"word": "Sociable",      "color": "orange"},
    {"word": "Charismatic",   "color": "orange"},
    {"word": "Outgoing",      "color": "orange"},
    {"word": "Collaborative", "color": "orange"},
    {"word": "Warm",          "color": "orange"},
    {"word": "Engaging",      "color": "orange"},
    {"word": "Inclusive",     "color": "orange"},
    {"word": "Persuasive",    "color": "orange"},
    # Yellow — The Optimist
    {"word": "Enthusiastic",  "color": "yellow"},
    {"word": "Optimistic",    "color": "yellow"},
    {"word": "Playful",       "color": "yellow"},
    {"word": "Inspiring",     "color": "yellow"},
    {"word": "Spontaneous",   "color": "yellow"},
    {"word": "Cheerful",      "color": "yellow"},
    {"word": "Energetic",     "color": "yellow"},
    {"word": "Vibrant",       "color": "yellow"},
    # Green — The Peacemaker
    {"word": "Loyal",         "color": "green"},
    {"word": "Patient",       "color": "green"},
    {"word": "Nurturing",     "color": "green"},
    {"word": "Empathetic",    "color": "green"},
    {"word": "Reliable",      "color": "green"},
    {"word": "Supportive",    "color": "green"},
    {"word": "Sincere",       "color": "green"},
    {"word": "Harmonious",    "color": "green"},
    # Teal — The Mediator
    {"word": "Balanced",      "color": "teal"},
    {"word": "Diplomatic",    "color": "teal"},
    {"word": "Healing",       "color": "teal"},
    {"word": "Adaptive",      "color": "teal"},
    {"word": "Mindful",       "color": "teal"},
    {"word": "Temperate",     "color": "teal"},
    {"word": "Centered",      "color": "teal"},
    {"word": "Serene",        "color": "teal"},
    # Blue — The Analyst
    {"word": "Analytical",    "color": "blue"},
    {"word": "Precise",       "color": "blue"},
    {"word": "Logical",       "color": "blue"},
    {"word": "Methodical",    "color": "blue"},
    {"word": "Thorough",      "color": "blue"},
    {"word": "Organized",     "color": "blue"},
    {"word": "Rational",      "color": "blue"},
    {"word": "Systematic",    "color": "blue"},
    # Indigo — The Strategist
    {"word": "Strategic",     "color": "indigo"},
    {"word": "Principled",    "color": "indigo"},
    {"word": "Deliberate",    "color": "indigo"},
    {"word": "Discerning",    "color": "indigo"},
    {"word": "Measured",      "color": "indigo"},
    {"word": "Perceptive",    "color": "indigo"},
    {"word": "Calculated",    "color": "indigo"},
    {"word": "Purposeful",    "color": "indigo"},
    # Purple — The Visionary
    {"word": "Intuitive",     "color": "purple"},
    {"word": "Visionary",     "color": "purple"},
    {"word": "Creative",      "color": "purple"},
    {"word": "Imaginative",   "color": "purple"},
    {"word": "Philosophical", "color": "purple"},
    {"word": "Artistic",      "color": "purple"},
    {"word": "Insightful",    "color": "purple"},
    {"word": "Idealistic",    "color": "purple"},
    # Gold — The Luminary
    {"word": "Poised",        "color": "gold"},
    {"word": "Noble",         "color": "gold"},
    {"word": "Graceful",      "color": "gold"},
    {"word": "Magnetic",      "color": "gold"},
    {"word": "Dignified",     "color": "gold"},
    {"word": "Distinguished", "color": "gold"},
    {"word": "Refined",       "color": "gold"},
    {"word": "Captivating",   "color": "gold"},
    # Rose — The Empath
    {"word": "Compassionate", "color": "rose"},
    {"word": "Tender",        "color": "rose"},
    {"word": "Sensitive",     "color": "rose"},
    {"word": "Caring",        "color": "rose"},
    {"word": "Affectionate",  "color": "rose"},
    {"word": "Devoted",       "color": "rose"},
    {"word": "Soulful",       "color": "rose"},
    {"word": "Attuned",       "color": "rose"},
]

MAX_WORD_SELECTIONS = 12   # cap per subject per rater

# ── Color profiles ────────────────────────────────────────────────────────────
COLOR_PROFILES = {
    "red": {
        "name": "Red", "archetype": "The Driver",
        "hex": "#E63946", "glow": "rgba(230,57,70,0.4)",
        "description": "You lead from the front. Bold, decisive, and results-focused — you see the goal and go. You don't wait for permission, you create momentum.",
        "strengths": ["Natural leader", "Decisive under pressure", "Relentless drive", "Fearless action"],
        "team_role": "You push the group forward and aren't afraid to make the hard call.",
    },
    "orange": {
        "name": "Orange", "archetype": "The Connector",
        "hex": "#FB5607", "glow": "rgba(251,86,7,0.4)",
        "description": "You make things happen through people. Charismatic, warm, and relentlessly social — you build the bridges, forge the alliances, and bring everyone into the fold.",
        "strengths": ["Magnetic presence", "Network builder", "Collaborative spirit", "Reads people instantly"],
        "team_role": "You are the glue — you know who to call and you make everyone feel included.",
    },
    "yellow": {
        "name": "Yellow", "archetype": "The Optimist",
        "hex": "#FFB703", "glow": "rgba(255,183,3,0.4)",
        "description": "You light up every room. Enthusiastic, inspiring, and contagiously positive — your energy pulls people in and makes the impossible feel achievable.",
        "strengths": ["Infectious enthusiasm", "Natural motivator", "Creative spark", "Sees possibilities everywhere"],
        "team_role": "You lift the energy when things get hard and make the work feel exciting.",
    },
    "green": {
        "name": "Green", "archetype": "The Peacemaker",
        "hex": "#2DC653", "glow": "rgba(45,198,83,0.4)",
        "description": "You are the steady heart of any team. Patient, loyal, and deeply empathetic — people trust you because you genuinely care and never waver.",
        "strengths": ["Deep loyalty", "Emotional intelligence", "Calming presence", "Long-term thinking"],
        "team_role": "You hold the group together and make sure no one gets left behind.",
    },
    "teal": {
        "name": "Teal", "archetype": "The Mediator",
        "hex": "#14B8A6", "glow": "rgba(20,184,166,0.4)",
        "description": "You are the bridge. Where others see conflict, you see a chance for understanding. Balanced and diplomatic, you bring people together and heal what's broken — without asking for credit.",
        "strengths": ["Natural diplomat", "Balanced perspective", "Healing presence", "Reads the room"],
        "team_role": "You de-escalate tension before anyone notices it's there and find the path everyone can walk.",
    },
    "blue": {
        "name": "Blue", "archetype": "The Analyst",
        "hex": "#4895EF", "glow": "rgba(72,149,239,0.4)",
        "description": "You see what others miss. Precise, logical, and thorough — you ask the right questions and make sure every decision is built on solid ground.",
        "strengths": ["Systematic thinking", "Eye for detail", "Clear reasoning", "Reliable execution"],
        "team_role": "You catch the errors before they become problems and keep everyone honest.",
    },
    "indigo": {
        "name": "Indigo", "archetype": "The Strategist",
        "hex": "#4338CA", "glow": "rgba(67,56,202,0.4)",
        "description": "You play chess while others play checkers. Principled, deliberate, and razor-sharp — you see ten moves ahead and you don't act until you're certain of the outcome.",
        "strengths": ["Long-range thinking", "Principled judgment", "Disciplined execution", "Strategic clarity"],
        "team_role": "You prevent the mistakes no one else saw coming and build the plan everyone else executes.",
    },
    "purple": {
        "name": "Purple", "archetype": "The Visionary",
        "hex": "#9B59F5", "glow": "rgba(155,89,245,0.4)",
        "description": "You see the world differently. Deeply intuitive and imaginative, you make connections no one else sees and you're always thinking several moves ahead.",
        "strengths": ["Big-picture thinking", "Creative intuition", "Philosophical depth", "Original ideas"],
        "team_role": "You bring the ideas that change the direction of the whole project.",
    },
    "gold": {
        "name": "Gold", "archetype": "The Luminary",
        "hex": "#C9A84C", "glow": "rgba(201,168,76,0.4)",
        "description": "You carry a natural authority that commands respect without demanding it. Poised and magnetic, you elevate every room you enter simply by being present.",
        "strengths": ["Natural gravitas", "Effortless presence", "Elevates others", "Timeless poise"],
        "team_role": "You set the tone. People look to you — not because you ask them to, but because they can't help it.",
    },
    "rose": {
        "name": "Rose", "archetype": "The Empath",
        "hex": "#F06090", "glow": "rgba(240,96,144,0.4)",
        "description": "You feel what others carry. Deeply compassionate and attuned, you notice the unspoken and you create space for people to be exactly who they are — and that changes everything.",
        "strengths": ["Deep emotional intelligence", "Unconditional care", "Creates psychological safety", "Soulful presence"],
        "team_role": "You make people feel seen. That trust is the invisible foundation every strong team is built on.",
    },
}

WORD_MAP = {w["word"]: w["color"] for w in WORDS}


# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                submitter    TEXT NOT NULL,
                subject      TEXT NOT NULL,
                words        TEXT NOT NULL,
                timings      TEXT NOT NULL DEFAULT '{}',
                submitted_at TEXT NOT NULL,
                PRIMARY KEY (submitter, subject)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quiz_times (
                submitter     TEXT PRIMARY KEY,
                total_seconds REAL NOT NULL,
                submitted_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS assignments (
                person         TEXT PRIMARY KEY,
                color          TEXT NOT NULL,
                score          REAL NOT NULL,
                all_scores     TEXT NOT NULL,
                top_words      TEXT NOT NULL DEFAULT '[]',
                self_submitted INTEGER NOT NULL DEFAULT 1
            )
        """)
        # Migrations for databases created before these columns existed
        for col, definition in [
            ("top_words",      "TEXT NOT NULL DEFAULT '[]'"),
            ("self_submitted", "INTEGER NOT NULL DEFAULT 1"),
        ]:
            try:
                conn.execute(f"ALTER TABLE assignments ADD COLUMN {col} {definition}")
            except Exception:
                pass
        for col, definition in [
            ("timings", "TEXT NOT NULL DEFAULT '{}'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE submissions ADD COLUMN {col} {definition}")
            except Exception:
                pass
        conn.commit()

init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────
def timing_weight(ms: int) -> float:
    """Per-word selection latency → score multiplier.
    Instantaneous picks (gut reaction) count more; slow/reconsidered picks count less.
    When 3+ people all immediately pick the same word for someone, that stacks to a
    very strong signal (e.g. 3 × 2.0 = 6.0 vs 3 × 0.5 = 1.5 for slow picks).
    """
    if ms < 2_000:  return 2.0   # gut reaction
    if ms < 5_000:  return 1.5   # quick
    if ms < 12_000: return 1.0   # considered
    if ms < 25_000: return 0.75  # hesitant
    return 0.5                    # afterthought


def quiz_time_bonus(total_seconds: float) -> dict[str, float]:
    """Small color bonus based on how quickly someone completed the quiz.
    Applied only to the submitter's own color score — their pace reveals their style.

    Fast (instinctive, decisive, energetic) → Red / Yellow / Purple / Orange
    Slow (deliberate, analytical, patient)   → Indigo / Blue / Green / Teal
    """
    if total_seconds < 60:
        return {"red": 1.5, "yellow": 1.0, "purple": 0.75, "orange": 0.5}
    if total_seconds < 120:
        return {"yellow": 0.75, "orange": 0.5, "purple": 0.25}
    if total_seconds > 480:   # 8+ minutes
        return {"indigo": 1.5, "blue": 1.0, "green": 0.75, "teal": 0.5}
    if total_seconds > 240:   # 4+ minutes
        return {"blue": 0.75, "indigo": 0.5, "green": 0.25}
    return {}                  # 2–4 minutes: neutral, no modifier


def compute_person_scores() -> dict:
    """Returns {person: {color: weighted_score}} aggregated from all submissions."""
    scores = {p: {c: 0.0 for c in COLOR_PROFILES} for p in PARTICIPANTS}

    with sqlite3.connect(DB_PATH) as conn:
        sub_rows  = conn.execute("SELECT subject, words, timings FROM submissions WHERE submitter != subject").fetchall()
        time_rows = conn.execute("SELECT submitter, total_seconds FROM quiz_times").fetchall()

    for subject, words_json, timings_json in sub_rows:
        if subject not in scores:
            continue
        timings = json.loads(timings_json) if timings_json else {}
        for word in json.loads(words_json):
            if word in WORD_MAP:
                weight = timing_weight(timings.get(word, 10_000))
                scores[subject][WORD_MAP[word]] += weight

    # Apply completion-time bonus to each person's own score
    for submitter, total_seconds in time_rows:
        if submitter not in scores:
            continue
        for color, bonus in quiz_time_bonus(total_seconds).items():
            scores[submitter][color] += bonus

    return scores


def assign_unique_colors(person_scores: dict,
                         people: list | None = None,
                         colors: list | None = None) -> dict:
    """Brute-force unique color assignment maximising total fit score.
    Optionally pass a subset of people and/or available colors so existing
    assignments can be preserved and only new people get assigned."""
    if people is None: people = list(PARTICIPANTS)
    if colors is None: colors = list(COLOR_PROFILES.keys())

    if not people:
        return {}

    # Single person — just pick their best available color
    if len(people) == 1:
        p = people[0]
        best = max(colors, key=lambda c: person_scores[p].get(c, 0))
        return {p: best}

    matrix = [[person_scores[p].get(c, 0) for c in colors] for p in people]
    best_total, best_perm = -1, None
    for perm in permutations(range(len(colors)), len(people)):
        total = sum(matrix[i][perm[i]] for i in range(len(people)))
        if total > best_total:
            best_total = total
            best_perm  = perm

    return {people[i]: colors[best_perm[i]] for i in range(len(people))}


def submitted_names() -> list[str]:
    """Names who have submitted ratings for ALL participants."""
    expected = len(PARTICIPANTS)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT submitter, COUNT(*) FROM submissions GROUP BY submitter"
        ).fetchall()
    return [name for name, cnt in rows if cnt == expected]


def is_calculated() -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] > 0


def generate_why_paragraph(color: str, top_words: list[dict]) -> str:
    words = [w["word"] for w in top_words]
    if not words:
        return ""
    if len(words) == 1:
        word_str = words[0]
    elif len(words) == 2:
        word_str = f"{words[0]} and {words[1]}"
    else:
        word_str = ", ".join(words[:-1]) + f", and {words[-1]}"

    paragraphs = {
        "red": (
            f"Your team kept coming back to the same picture of you: someone who moves. "
            f"The words {word_str} showed up because people see a person who doesn't wait for the moment to be right — "
            f"you create it. Red is assigned to the person the group looks to when a decision has to be made and no one else is stepping forward."
        ),
        "orange": (
            f"What your team noticed most wasn't a skill — it was a quality. "
            f"{word_str.capitalize()} came up because people genuinely feel something when they're around you. "
            f"Orange goes to the person who makes a room feel different just by being in it, "
            f"who builds relationships without trying, and who holds the group together in ways that don't always get named."
        ),
        "yellow": (
            f"Your team described someone who changes the energy. "
            f"{word_str.capitalize()} — these aren't small words. They describe the person who makes hard things feel possible, "
            f"who keeps momentum alive when it wants to stall. "
            f"Yellow belongs to the one who reminds everyone why the work matters in the first place."
        ),
        "green": (
            f"Your team sees someone they trust completely — and that trust is built on consistency. "
            f"{word_str.capitalize()} came up because people feel held by you. Not in a visible way, but in the way "
            f"that only becomes obvious when you're not there. Green belongs to the person the group leans on quietly, "
            f"the one who makes it safe for others to show up as they are."
        ),
        "teal": (
            f"Your team noticed that you hold the middle — the place most people avoid. "
            f"{word_str.capitalize()} kept appearing because people experience you as someone who doesn't take sides, "
            f"who sees the full picture, and who finds the path forward when others are stuck in their position. "
            f"Teal belongs to the person who makes hard conversations feel like progress instead of conflict."
        ),
        "blue": (
            f"Your team sees someone who gets things right. "
            f"{word_str.capitalize()} came up because people notice that you catch what others miss — "
            f"the assumption that wasn't tested, the gap in the plan, the detail that matters later. "
            f"Blue belongs to the person who builds the foundation that good decisions stand on, "
            f"even when no one realizes they needed it."
        ),
        "indigo": (
            f"Your team sees someone who thinks before they act — and it shows in the quality of what you do. "
            f"{word_str.capitalize()} surfaced because people recognize that you're playing a longer game. "
            f"You're not reacting to what's in front of you; you're accounting for what's coming. "
            f"Indigo belongs to the person the group trusts to build a plan that actually holds up."
        ),
        "purple": (
            f"Your team sees a mind that works differently. "
            f"{word_str.capitalize()} kept coming up because people notice that you connect things no one else connects — "
            f"that you find the question behind the question, the possibility inside the problem. "
            f"Purple belongs to the person whose idea, once heard, makes everyone wonder how they didn't see it before."
        ),
        "gold": (
            f"Your team noticed something that's hard to define but impossible to miss. "
            f"{word_str.capitalize()} came up because people experience a presence when you're in the room — "
            f"a kind of gravity that shapes how the group carries itself. "
            f"Gold belongs to the person who earns respect without demanding it, "
            f"who sets the tone simply by being present."
        ),
        "rose": (
            f"Your team sees someone who pays attention in a way most people don't. "
            f"{word_str.capitalize()} came up because people feel genuinely seen around you — "
            f"noticed, not just acknowledged. "
            f"Rose belongs to the person whose care creates the kind of safety that makes real work possible, "
            f"the invisible foundation that everything else is built on."
        ),
    }
    return paragraphs.get(color, "")


def compute_top_words(person: str, color: str) -> list[dict]:
    """Returns top-4 words (by selection count) that map to the assigned color."""
    color_words = {w["word"] for w in WORDS if w["color"] == color}
    counts: dict[str, int] = {}
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT words FROM submissions WHERE subject = ?", (person,)
        ).fetchall()
    for (words_json,) in rows:
        for word in json.loads(words_json):
            if word in color_words:
                counts[word] = counts.get(word, 0) + 1
    return [
        {"word": w, "count": c}
        for w, c in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:4]
        if c > 0
    ]


# ── API ───────────────────────────────────────────────────────────────────────
@app.get("/api/participants")
def get_participants():
    return {"participants": PARTICIPANTS}


@app.get("/api/words")
def get_words():
    words = [w["word"] for w in WORDS]
    random.shuffle(words)
    return {"words": words, "max_selections": MAX_WORD_SELECTIONS}


@app.get("/api/status")
def get_status():
    done    = submitted_names()
    pending = [p for p in PARTICIPANTS if p not in done]
    with sqlite3.connect(DB_PATH) as conn:
        time_rows = conn.execute("SELECT submitter, total_seconds FROM quiz_times").fetchall()
    quiz_times_map = {r[0]: r[1] for r in time_rows}
    return {
        "total":      len(PARTICIPANTS),
        "submitted":  done,
        "pending":    pending,
        "all_done":   len(pending) == 0,
        "calculated": is_calculated(),
        "quiz_times": quiz_times_map,   # { name: total_seconds }
    }


class Rating(BaseModel):
    subject: str
    words:   list[str]
    timings: dict[str, int] = {}

class SubmitRequest(BaseModel):
    submitter: str
    ratings:   list[Rating]
    total_ms:  int = 0   # total milliseconds from quiz start to submit button


@app.get("/api/my-ratings/{name}")
def get_my_ratings(name: str):
    """Returns which subjects this person has already rated."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT subject FROM submissions WHERE submitter = ?", (name,)
        ).fetchall()
    return {"rated": [r[0] for r in rows]}


@app.post("/api/submit")
def submit(req: SubmitRequest):
    if req.submitter not in PARTICIPANTS:
        raise HTTPException(400, "Unknown participant")
    now = datetime.now().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        for rating in req.ratings:
            conn.execute(
                """INSERT OR REPLACE INTO submissions
                   (submitter, subject, words, timings, submitted_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (req.submitter, rating.subject,
                 json.dumps(rating.words), json.dumps(rating.timings), now),
            )
        if req.total_ms > 0:
            conn.execute(
                "INSERT OR REPLACE INTO quiz_times VALUES (?, ?, ?)",
                (req.submitter, req.total_ms / 1000.0, now),
            )
        conn.commit()
    return {"ok": True}


@app.post("/api/calculate")
def calculate():
    person_scores = compute_person_scores()
    submitted     = set(submitted_names())

    with sqlite3.connect(DB_PATH) as conn:
        existing = dict(conn.execute("SELECT person, color FROM assignments").fetchall())

    # Only assign people who don't have a color yet, from remaining colors
    unassigned    = [p for p in PARTICIPANTS if p not in existing]
    used_colors   = set(existing.values())
    avail_colors  = [c for c in COLOR_PROFILES if c not in used_colors]

    new_assignment = assign_unique_colors(person_scores, people=unassigned, colors=avail_colors)

    with sqlite3.connect(DB_PATH) as conn:
        for person, color in new_assignment.items():
            score          = person_scores[person].get(color, 0)
            top_words      = compute_top_words(person, color)
            self_submitted = 1 if person in submitted else 0
            conn.execute(
                """INSERT OR REPLACE INTO assignments
                   (person, color, score, all_scores, top_words, self_submitted)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (person, color, score, json.dumps(person_scores[person]),
                 json.dumps(top_words), self_submitted),
            )
        conn.commit()

    all_assignments = {**existing, **new_assignment}
    return {"ok": True, "assignments": all_assignments}


@app.get("/api/assignments")
def get_assignments():
    if not is_calculated():
        return {"calculated": False, "assignments": []}
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT person, color, score, all_scores, top_words, self_submitted FROM assignments"
        ).fetchall()
    result = []
    order  = {p: i for i, p in enumerate(PARTICIPANTS)}
    for person, color, score, all_scores, top_words, self_submitted in rows:
        parsed_words = json.loads(top_words)
        result.append({
            "person":         person,
            "color":          color,
            "score":          score,
            "all_scores":     json.loads(all_scores),
            "top_words":      parsed_words,
            "why_text":       generate_why_paragraph(color, parsed_words),
            "self_submitted": bool(self_submitted),
            "profile":        COLOR_PROFILES[color],
        })
    result.sort(key=lambda x: order.get(x["person"], 99))
    return {"calculated": True, "assignments": result}


@app.get("/api/result/{name}")
def get_result(name: str):
    if not is_calculated():
        raise HTTPException(404, "Colors not yet assigned")
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT color, score, all_scores, top_words, self_submitted FROM assignments WHERE person = ?",
            (name,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    color, score, all_scores, top_words, self_submitted = row
    parsed_words = json.loads(top_words)
    return {
        "person":         name,
        "color":          color,
        "profile":        COLOR_PROFILES[color],
        "all_scores":     json.loads(all_scores),
        "top_words":      parsed_words,
        "why_text":       generate_why_paragraph(color, parsed_words),
        "self_submitted": bool(self_submitted),
    }


@app.get("/api/export")
def export_submissions():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT submitter, subject, words, timings, submitted_at FROM submissions ORDER BY submitted_at"
        ).fetchall()
    return {
        "exported_at": datetime.now().isoformat(),
        "submissions": [
            {"submitter": r[0], "subject": r[1], "words": json.loads(r[2]),
             "timings": json.loads(r[3]) if r[3] else {}, "submitted_at": r[4]}
            for r in rows
        ],
    }


class ImportRequest(BaseModel):
    submissions: list[dict]


@app.post("/api/import")
def import_submissions(req: ImportRequest):
    imported = 0
    with sqlite3.connect(DB_PATH) as conn:
        for s in req.submissions:
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO submissions
                       (submitter, subject, words, timings, submitted_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (s["submitter"], s["subject"],
                     json.dumps(s["words"]),
                     json.dumps(s.get("timings", {})),
                     s.get("submitted_at", datetime.now().isoformat())),
                )
                imported += 1
            except Exception:
                pass
        conn.commit()
    return {"ok": True, "imported": imported}


@app.delete("/api/reset")
def reset():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM submissions")
        conn.execute("DELETE FROM assignments")
        conn.execute("DELETE FROM quiz_times")
        conn.commit()
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_PATH, html=True), name="static")
