# Codex instructions for 配信カルテ

## Product goal

Build a helpful VTuber stream review tool. The tone should be constructive and slightly playful, never insulting or punitive.

## Current state

This folder is a dependency-free UI prototype. `index.html` contains all HTML, CSS, and JavaScript. All diagnosis values are mock data.

## Design constraints

- Japanese-first UI
- Dark, modern dashboard style
- Responsive down to 360px width
- Avoid external assets and libraries unless a later task explicitly asks for them
- Clearly label predicted values versus measured YouTube Analytics values
- Never imply that a mock score was produced from a real video

## Suggested next architecture

- Frontend: Next.js + TypeScript
- API route: create analysis job and return job id
- Worker: video/audio/transcript analysis
- Storage: PostgreSQL or Supabase
- Auth: Google OAuth for channel-owner-only analytics

## Verification

For the static prototype:

```bash
python3 -m http.server 4173
```

Open `http://localhost:4173`, test the demo button, invalid URL validation, result display, retry, and copy button.
