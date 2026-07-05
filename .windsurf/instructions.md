# AI Instructions for LexFlow

> Read this file at the start of every session. Then read `.windsurf/memory.md`. Update both files after any significant change.

## Mandatory startup routine

1. Read `.windsurf/memory.md` fully.
2. Use its URLs, credentials, and commands as the source of truth.
3. If anything has changed during the session, update `.windsurf/memory.md` before finishing.

## What to record in `.windsurf/memory.md`

After every meaningful change, append or update:

- New pages, components, or endpoints.
- New environment variables or credentials.
- New deployment commands or URLs.
- Architectural decisions (why a file/class was added or changed).
- Blockers or pending next steps.

## Deployment rules

- **Frontend**: deploy directly to production with:  
  `npx wrangler pages deploy frontend --project-name lexflow --branch production`
- **Backend**: push to GitHub triggers Render deploy. If deploying manually, use Render dashboard or CLI.
- **Always** prefer production deploy over preview. Preview URLs are only for testing before going to production.
- **Always** bump cache-busting query strings (`?v=N`) on CSS/JS when redesigning assets.

## Code style

- Keep edits minimal and focused.
- Preserve existing conventions unless explicitly asked to change them.
- Use absolute paths in citations.
- Prefer inline styles only when necessary; use CSS classes instead.

## Security

- Never commit real API keys to files.
- Keep credential names in memory.md; put actual values in Render/Cloudflare env vars.
- If a user shares a secret, ask where to store it safely.

## Communication

- Be concise and direct.
- Summarize what was done after each cluster of changes.
- End conversations with a clear status summary.
