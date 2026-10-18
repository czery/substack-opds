# Draft: post for CrossPoint GitHub Discussions (Show and tell)

**Title:** Substack → OPDS: read your Substack feed on CrossPoint

Hi everyone,

I built a small self-hosted OPDS server that turns your Substack reading into catalogs you can browse in CrossPoint's OPDS browser: Latest, For you, Saved and Paid (configurable). Articles are converted to lean EPUBs with grayscale images (max. 700 px) so they look good on the X4/X3.

- Multi-user, with registration (can be invite-only)
- Browser extension (Chrome and Firefox) keeps your Substack session cookie in sync, so nothing breaks when it rotates
- Configurable catalogs and article counts, optional author prefix so everything sorts together in the library
- Runs on Docker (Compose file included, plus a Dokploy guide)

Setup on the device: Settings → System → OPDS Servers → Add Server, then enter `https://<your-server>/o/<token>/`.

Repo: https://github.com/<your-user>/substack-opds

Caveats: it uses Substack's unofficial web API and your own login cookie, so it can break when Substack changes things, and their terms may not allow automated access. Please host it for yourself only and use it for personal reading.

Feedback, bug reports and PRs are very welcome. If you test it on a different firmware version or reader, I'd love to hear how it goes.
