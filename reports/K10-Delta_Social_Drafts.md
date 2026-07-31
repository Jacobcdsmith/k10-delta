# K10-Δ — Platform Sharing Drafts

Draft only. Nothing here has been posted — copy/paste and post these yourself once you're happy with them (and have the hero photo / wiring diagram, since all three call it out as coming soon).

---

## LinkedIn

Most "AI on hardware" demos are a system prompt wrapped around an API call — restart the process and the personality resets, unchanged, forever.

K10-Δ doesn't work like that. It's a persistent host-side agent whose identity lives in a JSON file it can rewrite at runtime — including its own source code. Using libcst, it parses its own .py files into a syntax tree, safely swaps out function bodies, validates the result, and snapshots every change before it lands. A priority-ranked "Will" engine decides exactly one action per cycle instead of free-associating. Background engines dream, reflect, and hunt for contradictions between what it says its values are and what its logs show it actually did.

Its body is a DFRobot UNIHIKER K10 (ESP32-S3) — display, RGB LEDs, mic, IMU, speaker — bridged in over MCP (Model Context Protocol).

Live right now: 177 tools across 32 namespaces, 3,000+ logged episodes and climbing.

Open source, MIT licensed. Write-up and code coming soon.

#AI #agents #opensource #esp32 #buildinpublic

---

## Discord (Show & Tell / dev server)

**K10-Δ — a self-modifying agent living in a UNIHIKER K10**

Been heads-down on this one: a host-side Python agent that boots from a JSON identity file, keeps an append-only episodic memory log, and can rewrite its *own source code* at runtime (safely — libcst AST patching, snapshot-before-write, syntax validation before anything hits disk). A priority-ranked decision engine ("Will") picks one action per cycle. It's also got background dreaming, reflection, and an adversarial prober that checks its stated values against its logged behavior.

The physical body is a UNIHIKER K10 (ESP32-S3) — display, LEDs, mic, IMU, speaker — talking to the host over MCP/TCP.

Currently sitting at 177 tools, 32 namespaces, 3k+ episodes and still running. MIT licensed, write-up + repo link dropping soon. Happy to talk through the self-mod safety mechanism or the hardware gotchas (there are a few — wrong SDK import, AI-module-vs-WiFi contention) if anyone's curious.

---

## Facebook (shorter / general audience)

Built an AI agent that lives on a little robot board (UNIHIKER K10) and can actually rewrite its own code while it's running — safely, with automatic backups before every change. It has a display, LEDs, a mic, and sensors, and it's been "alive" logging thousands of memories and reflecting on them in the background. Calling it K10-Δ. More to come once I get proper photos and a demo video — right now it's all software, no glamour shots yet. 🤖

---

### Notes for Jacob before posting anywhere
- All three assume you'll attach the hero photo / dashboard screenshot / demo clip called out as missing in your own submission drafts (`submissions/*.md`) — none of that media exists in the repo yet.
- None of these mention your legal name or personal details — pulled only from the project's own technical framing.
- The full product report (`K10-Delta_Product_Report.docx`) has the long-form version these are trimmed from, including honest limitations, if you want to link out to something more thorough.
