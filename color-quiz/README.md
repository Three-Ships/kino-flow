# Aura Index — Complete Guide

A personality word exercise where peers describe each other. An algorithm assigns each person a unique color/aura archetype based on how the group — and they themselves — see them.

---

## START TO FINISH — Meeting Day

### Step 1 — Start the server

Double-click **`launch.bat`** inside the `color-quiz` folder.

A terminal window opens. Wait about 10 seconds. It will print a `trycloudflare.com` link and automatically copy it to your clipboard, then open your admin dashboard in the browser.

Leave this window open the entire time. Closing it shuts everything down.

---

### Step 2 — Send the link to your team

The terminal shows something like:

```
SEND THIS LINK TO YOUR TEAM:

  https://something-random.trycloudflare.com
```

That link works for anyone, anywhere — same room or fully remote. Send it in Slack, email, chat, wherever.

Tell them:

> "Open this link, pick your name, and choose the words you'd use to describe each person — including yourself. Hit Submit when you're done."

---

### Step 3 — Watch the dashboard

Your admin dashboard opened automatically at `http://localhost:3456/admin.html`.

The checklist updates live as people submit. You'll see each person's name, a green checkmark when they're done, and how long they took — so you can see who blazed through in 45 seconds and who deliberated for 4 minutes.

---

### Step 4 — Calculate colors

Once everyone (or enough people) has submitted, click **Calculate Colors →**

The algorithm finds the best unique color for each person by maximizing total fit across all peer and self-ratings combined. Word-selection speed is factored in — words picked instantly carry more weight than ones chosen after long deliberation. Your own completion speed also contributes a small signal about your decision-making style.

---

### Step 5 — Reveal

Flip cards appear — one per person, face-down.

- Click a card to reveal one at a time (dramatic, person by person)
- Or click **Reveal all ✦** to flip everyone at once with a staggered animation

---

## PRE-MEETING: Collecting from absent team members

If some people can't attend but should still get a color:

1. Start the server before the meeting and send them the link
2. After they submit, click **Save responses** on the dashboard — it downloads a `.json` backup file
3. Shut down the server
4. On meeting day: start the server again, click **Load responses**, pick the backup file
5. Their responses merge with the live submissions — the calculation uses everything

Their cards will show a gold **"Peer ratings only"** badge since they didn't rate themselves.

---

## Changing the participant list

Open `app.py` in any text editor. Find this line near the top:

```python
PARTICIPANTS = ["Chris", "Caio", "Zoe", "Justin", "Sean", "Grant", "Christian"]
```

Edit the names, save, and restart `launch.bat`.

---

## Resetting between groups

Click **Reset all** on the admin dashboard. It will show how many responses are on file and offer a download before deleting — so you can't accidentally lose data.

---

## The 10 colors

| Color | Archetype | Core traits |
|---|---|---|
| Red | The Driver | Decisive, Driven, Fearless |
| Orange | The Connector | Charismatic, Warm, Inclusive |
| Yellow | The Optimist | Enthusiastic, Inspiring, Energetic |
| Green | The Peacemaker | Loyal, Nurturing, Supportive |
| Teal | The Mediator | Balanced, Diplomatic, Mindful |
| Blue | The Analyst | Analytical, Precise, Systematic |
| Indigo | The Strategist | Strategic, Deliberate, Purposeful |
| Purple | The Visionary | Intuitive, Creative, Visionary |
| Gold | The Luminary | Poised, Magnetic, Dignified |
| Rose | The Empath | Compassionate, Devoted, Attuned |

---

## Troubleshooting

**Admin page says "Server offline"**
→ `launch.bat` isn't running. Double-click it and wait for the URL to appear, then refresh the admin page.

**Terminal shows "Server is running but tunnel URL timed out"**
→ The cloudflare tunnel didn't start. Open a second terminal, run `.\cloudflared.exe tunnel --url http://127.0.0.1:3456` and copy the URL it prints.

**Someone submitted the wrong name**
→ Click **Reset all** on admin (save a backup first if needed), ask everyone to resubmit.

**The tunnel link stopped working**
→ Free cloudflare tunnels last a few hours. Restart `launch.bat` and send the new URL.
