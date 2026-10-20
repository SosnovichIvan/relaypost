# ADR 002: personal Telegram sessions

Implementation of the owner's accepted scenario, 2026-10-20. Bot API remains the
control interface; a dedicated MTProto adapter owns personal sessions. Keeping
only Bot API cannot implement personal account publishing; TDLib adds a native
runtime, so use pinned Telethon with raw requests for explicit random_id control.

One Telegram owner may connect multiple personal accounts; each has multiple
verified channels/groups and its own story destination. Never assume account
authorization grants channel admin rights. Verify capabilities again before each
publication. Store StringSession encrypted with the existing Fernet vault in the
separate personal_sessions table (PK/FK account_id, encrypted BLOB). The legacy
accounts row contains platform=telegram and an encrypted empty placeholder; new
Store projections expose telegram-user only when the private record exists.
This prevents v0.1's unknown-platform fallback from sending a personal session to
VK on rollback: the legacy Telegram token validator rejects the empty placeholder
before any network I/O. A DB trigger prevents legacy account deletion from losing
the session without revocation; the new application explicitly deletes both rows
after remote logout in one transaction. Credential-role collisions are rejected.
No OTP
or 2FA input in bot messages. /disconnect revokes the remote session before local
deletion; unsuccessful revocation preserves the encrypted credential for retry.

QR or phone/code authorization uses an HTTPS page with short-lived random owner-bound tickets,
browser binding, CSRF, no third-party assets, no logs or cache, origin-only Referer. The
user explicitly starts login and confirms the displayed account before saving.
Temporary sessions exist only in memory; cancellation, expiry and shutdown attempt
remote logout and disconnect. Network failures may require manual device revocation.
A restart invalidates incomplete links. 2FA exists only in
the POST body and is never persisted. A session is a broad account credential,
not a scoped publishing token; explain this before login. Private encrypted
backups still contain session credentials and need access protection.

Use vpn_proxy_server through HTTP CONNECT for MTProto TCP on port 443. Do not
reuse the HTTPX mount as if it routed arbitrary TCP; configure Telethon's HTTP
proxy explicitly. No direct fallback. API_ID/API_HASH belong to our application,
not to every user. Require a real HTTPS origin and API secrets before enabling
the login endpoint. Listen inside the container, expose only host loopback for an
HTTPS reverse proxy. Live DC connectivity and auth require separate acceptance.

Multiple selection is scoped to a persisted owner draft. Toggle destinations,
show an explicit selected list, then confirm. Within one batch deduplicate the
same Telegram peer across accounts by choosing one account explicitly. Claims
persist before network I/O. Record each destination independently; do not retry
successful or ambiguous sends when another destination fails. Changing selection
requires another confirmation; no request may publish an unconfirmed selection.

Schema uses additive, idempotently created namespaced selection tables,
keeping core user_version=1 for compatibility with the support PR (which adds v2).
Existing core tables are unchanged. New data cascades with draft expiry. Single
process / one poller remains required; all network I/O occurs outside SQLite
transactions. No load or HA claims. PR CI requires integration tests, separate
90% line/branch thresholds and container build. Live publication is not performed
by tests. Rollout: feature → develop → main → tag, pre-deploy backup, HTTPS proxy,
credentials and authorized test account verification before enabling pilot.

## Operator setup and user flow

Required repository Secrets: TELEGRAM_API_ID and TELEGRAM_API_HASH from our
application at my.telegram.org. Required repository Variable RELAYPOST_LOGIN_ORIGIN
is a dedicated HTTPS origin, with no path/query/credentials, e.g.
https://login.example.org. Until these settings and valid TLS exist, personal
Telegram login stays disabled; do not describe the running 0.1.0 as upgraded.
Store the same values in private local .env for a controlled developer launch.
No API_HASH, StringSession, login URL or 2FA password belongs in issues or logs.

Production Compose exposes only 127.0.0.1:18080 for the login app. The internal
listener is 8080; the application starts it only with a configured origin.
Use deploy/login.nginx.conf.example on the VPS after replacing AUTH_DOMAIN and
installing a real certificate. Disable URL/body logging at every proxy/CDN layer
and request buffering to disk. Never expose the internal plaintext listener on
a public interface. The HTTPS template is not installed or certificate-validated
until an actual domain is supplied. Application and proxy need no other users'
API credentials. Existing Bot API traffic retains its Telegram-only HTTPX mount.

1. /login in a private bot dialog. Open the issued private link (10 minutes).
2. Read the broad-access notice and check the destination RelayPost owner ID.
   Either enter your phone and the code on this HTTPS page (works on one phone),
   or show QR and scan in Telegram Devices from another screen. Telegram chooses
   code delivery; SMS is not guaranteed for third-party apps. A payment-required
   code response is rejected without payment and suggests QR login instead.
3. Enter 2FA on HTTPS only if requested. At most 3 code submissions and 3 password
   submissions per attempt; new login attempts are limited to one per owner per
   30 seconds. Phone and phone-code hash remain in memory only until login;
   OTP and password are never persisted, echoed, logged or sent through bot messages.
4. Confirm the displayed personal account before saving the encrypted session.
   Reconnecting an existing account revokes its old session before replacing it.
5. /accounts; /discover ACCOUNT_ID checks up to 100 dialogs and adds eligible
   administered channels and writable joined groups; /add ACCOUNT_ID @channel works for individual entries.
   Numeric peers must be in that bounded dialog cache. /addstory ACCOUNT_ID me
   adds your own story destination; a channel ID adds a channel story if permitted.
6. Send text/photo/video. Publish to one place explicitly, or use selection
   buttons, review and confirm a batch (up to 20 places). Lists paginate 20 at a
   time. Choosing a different account for the same peer replaces the old choice.
   Personal stories use contacts-only privacy and 24 hours, shown before sending;
   channel story audience follows Telegram's channel rules. Premium/boost and
   posting limits are enforced through canSendStory and Telegram responses.
7. /status shows individual deliveries. /disconnect ACCOUNT_ID logs out the
   remote personal session before deleting its local credential and destinations.
   Already-revoked sessions can be removed; transient revocation errors keep the
   encrypted credential for retry. If reconnect changes the credential while
   logout is in flight, deletion is refused and the new session remains available
   for explicit retry. /cancel invalidates your incomplete login links; cancelling
   an already-saved browser form does not silently claim to disconnect the account.

Batch sends run under one active batch lock while polling continues; background
work is cancelled and joined before closing the DB and HTTP client. Each session
serializes requests and logout using a lock so an active write finishes before
revocation. A crash marks claimed attempts unknown on restart; unattempted batch
destinations are not automatically resumed. No blind resend of unknown/sent writes.
The peer guard backfills from existing delivery rows to prevent alias duplicates
across an upgrade. On rollback, close the HTTPS authorization route; old v1 code
continues legacy operations but cannot publish with or delete personal sessions.
Its deletion attempt encounters the DB guard, so use a forward fix to manage those
accounts. Do not remove the session table/trigger or downgrade personal credentials
into the legacy secret column. Database backups must retain the new encrypted table.

Acceptance: real SQLite, fake Telegram and local HTTP login integration; live QR,
2FA and real posts/stories still require credentials and designated test accounts.
On 2026-10-20 a token-free CONNECT from the existing private Squid to Telegram
DC 149.154.167.51:443 succeeded. This verifies the upstream proxy path, not a live
MTProto login or the full RelayPost VPS tunnel. SIGKILL can lose an incomplete
in-memory session; revoke an abandoned RelayPost device in Telegram if necessary.

The current QR browser binding intentionally does not support switching browsers
mid-login. A transferred login link is a bearer credential: verify the RelayPost
owner ID and never scan a QR supplied by another person. No login token or password
is included in telemetry. The cryptg wheel accelerates MTProto media encryption;
media processing, cropping and transcoding are not implemented.

Sources checked: [authorization](https://core.telegram.org/api/auth),
[QR](https://core.telegram.org/api/qr-login),
[stories](https://core.telegram.org/method/stories.sendStory),
[Telethon](https://docs.telethon.dev/en/stable/modules/client.html).
