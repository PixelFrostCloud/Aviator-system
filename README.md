# Pakabet (Django)

Professional Django web application built with Django, HTML, CSS, JavaScript, and Opaypesa wallet funding.

## Features

- **User Authentication**: Registration and login plus one-time, expiring email password-reset links
- **Pakabet Crash Game**: Interactive betting interface with real-time multiplier animation and cash-out functionality
- **Custom chat moderation**: Admin-managed abusive-word and phrase filters block messages before saving and create review alerts in the custom operations dashboard
- **Wallet Management**: Opaypesa collections, disbursements, status checks, and transaction history
- **Referral Programme**: Personal invite links automatically attribute new registrations; inviters receive a platform-funded, withdrawable 5% of each referred player's first successful deposit
- **Admin Analytics Dashboard**: Comprehensive platform monitoring and user statistics
- **Multiplayer Rooms**: Real-time room support with WebSocket synchronization
- **Audit Logging**: Complete system activity tracking with admin reporting interface
- **Responsive Design**: Clean templates that work across all devices

## Payment Integration

New wallet requests use Opaypesa by default. The integration uses:

- Mobile money collections: `POST /api/v1/checkout/payment-intents`
- Mobile money disbursements: `POST /api/v1/checkout/disbursements`
- Transaction status lookup: `GET /api/v1/transactions/{transaction_id}`
- Server-to-server authentication with `X-API-Key`

The wallet accepts only MTN Mobile Money and Airtel Money in Uganda. Deposits must be between 500 and 10,000,000 UGX per transaction. Withdrawals start at 500 UGX and are limited by the player's available balance. Self-exclusion blocks deposits and new bets for the selected period.

Deposits send a mobile-money prompt to the submitted phone. A request is not treated as paid until Opaypesa returns `successful` from a status check; `pending` requests remain uncredited. Withdrawals are submitted as a one-item disbursement batch, and the amount stays on hold until a final provider status is confirmed. Failed or cancelled withdrawals automatically refund the hold, with a separate refund ledger row. Opaypesa status refresh is available from the wallet payment history; sandbox outcomes must first be simulated through Opaypesa's documented sandbox flow. If a submission times out before Opaypesa returns a transaction ID, the app does not resubmit it on refresh or allow another same-type request; support must reconcile it with Opaypesa to avoid duplicate charges or payouts. The integration does not currently accept Opaypesa callbacks: the published guide does not specify their signature-verification format, so wallet balances are updated only from authenticated server-side status lookups. A provider-reported reversal after a payment was already completed requires manual reconciliation; the app does not automatically debit a player's wallet.

Use an Opaypesa sandbox API key (`opaypesa_sbox_…`) for initial testing. Sandbox requests do not move real funds. Set `PAYMENT_GATEWAY=opaypesa`, `OPAYPESA_MODE=sandbox`, and `OPAYPESA_API_KEY` in the private `.env`. For production, use an approved live key (`opaypesa_live_…`), set `OPAYPESA_MODE=live`, and run with `DJANGO_DEBUG=False`, PostgreSQL, and `DJANGO_SECURE_SSL_REDIRECT=True`. Live Opaypesa mode refuses to start with SQLite or without HTTPS redirects because wallet balance locking and session security are required for real-money transfers. The app rejects mismatched key prefixes and requires HTTPS for Opaypesa API requests.

ioTec Pay's integration and configuration are retained for a future migration, but are not used for new payments while `PAYMENT_GATEWAY=opaypesa`. The ioTec API specifications remain in `Payment APIs/`.

Referral links are attributed when opened and remain available for registration for 30 days. A referral reward is credited once, after the referred player's first successful deposit is confirmed. It is funded by Pakabet, added to the inviter's normal withdrawable wallet balance, and never deducted from the referred player's deposit.

Password reset requests are sent to the account's registered email address. Reset links expire after one hour and can only be used once. Local development prints reset emails to the runserver console. Configure an SMTP provider in production:

```powershell
$env:EMAIL_HOST='smtp.example.com'
$env:EMAIL_PORT='587'
$env:EMAIL_USE_TLS='True'
$env:EMAIL_HOST_USER='your-smtp-user'
$env:EMAIL_HOST_PASSWORD='your-smtp-password'
$env:DEFAULT_FROM_EMAIL='Pakabet <no-reply@yourdomain.com>'
```

## Provably Fair rounds

Players can set or securely generate their client seed in the in-game Provably Fair settings. The saved seed is used for future rounds only; every round stores its own client seed and server-seed hash before play. Newly generated shared-game rounds are capped at 286.00x. The server seed is revealed after the round ends, and the Provably Fair page independently checks the commitment hash and crash-point calculation for each of the player's rounds; older rounds retain compatibility with the previous uncapped calculation.

## Payment environment

To test Opaypesa, add your sandbox key to `.env`:

```dotenv
PAYMENT_GATEWAY=opaypesa
OPAYPESA_MODE=sandbox
OPAYPESA_API_KEY=opaypesa_sbox_your_key
```

For live Opaypesa transactions after sandbox testing and merchant approval:

```dotenv
PAYMENT_GATEWAY=opaypesa
OPAYPESA_MODE=live
OPAYPESA_API_KEY=opaypesa_live_your_key
DJANGO_DEBUG=False
DJANGO_SECURE_SSL_REDIRECT=True
DATABASE_URL=postgresql://...
```

Keep the Opaypesa API key private. Do not configure Opaypesa webhooks to settle wallet requests until their signature format has been confirmed and implemented. The existing `/api/payments/callback/` endpoint is retained for ioTec Pay only.

To switch back to ioTec in the future, set `PAYMENT_GATEWAY=iotec_pay` and provide its configured credentials. Live ioTec withdrawals remain queued until the scheduled `process_queued_withdrawals` command is running and the ioTec wallet has sufficient UGX available balance.

## Environment configuration

Copy the example configuration into a private local environment file:

```powershell
Copy-Item .env.example .env
```

Generate a unique key and replace the example value of `DJANGO_SECRET_KEY` in `.env` before starting the app:

```powershell
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Keep `.env` private; it is excluded from Git. Put email and payment credentials in `.env` rather than source code or shell history.

## Security monitoring and incident response

Configure security-alert recipients using `DJANGO_ADMINS` in the deployment environment (comma-separated email addresses), and configure a working SMTP backend for production. Repeated login failures from one connection address are emailed after 5 failures in 10 minutes; further alerts for that address are suppressed for 15 minutes. Adjust these windows with `SECURITY_LOGIN_FAILURE_THRESHOLD`, `SECURITY_LOGIN_FAILURE_WINDOW_SECONDS`, and `SECURITY_ALERT_COOLDOWN_SECONDS`. The first request with a fresh audit history records a configuration baseline; on subsequent process starts, changes to monitored security settings generate an email alert.

Authentication successes and failures, HTTP 403 permission denials, and changes to monitored security settings are recorded as security events in the existing audit log. Security-setting snapshots include safe values such as `DEBUG`, allowed hosts, HTTPS flags, payment mode, and whether payment, callback, and email credentials are configured. They never include secret values. Security events record the connection peer address and a resolved route pattern; they do not record submitted credentials, request bodies, callback tokens, or payment references. Deposit and withdrawal audit entries intentionally omit amounts and provider references. Older audit entries created before this change may still contain payment references; access remains restricted, so apply your retention policy before exporting or sharing historical logs. Payment records still contain transaction details needed to operate the wallet and are not security logs.

Login-failure aggregation uses the direct connection peer address (`REMOTE_ADDR`) to avoid trusting spoofable forwarded headers. Behind a reverse proxy, multiple players may appear under the proxy address; account for this when interpreting alerts and configure trusted-proxy address handling before relying on source-address attribution.

Audit logs are read-only and visible only to superusers in Django Admin. Limit superuser accounts to named operators, protect their mailboxes and devices with MFA where available, and restrict production host/container log access to authorized operators. Do not enable request-body or credential logging at the reverse proxy, application server, or hosting provider. Retain and back up audit logs according to your privacy and legal requirements.

### Anti-cheat and abuse monitoring

Game bets are validated and debited against the locked server-side profile balance. Every bet must include a unique 32-character hexadecimal `request_id`; repeating the same request with the same settings is idempotent, while reusing it with changed settings or in a different round is rejected and recorded. Cash-out multipliers and payouts are calculated from server time, and settled game sessions and room players cannot be paid twice. Client-supplied balances, crash points, multipliers, or room outcomes are never authoritative.

The default request limits are 30 bets/account and 90 bets/IP per minute, 30 cash-outs/account and 120 cash-outs/IP per minute, 5 registrations/IP per hour, 8 payment requests/account and 20/IP per hour, 10 reward claims/account and 30/IP per hour, and 5 room creations/account and 20/IP per hour. Login attempts are limited to 10/account and 50/IP per 15 minutes. Room actions are limited to 20/account and 100/IP per minute per action. Configure the login, bet, cash-out, registration, payment, and room-creation limits with the corresponding `SECURITY_*` variables in `.env`; reward-claim and room-action limits are currently fixed in the application. Rate-limit events are persisted with the security audit records, so apply the same database availability, access, and retention protections as for other audit data.

The application records investigation signals for unusually high account creation or payment-request volume, reward claims sharing an IP or browser user-agent, conflicting reuse of bet identifiers, denied room access, invalid WebSocket messages, and excessive WebSocket reconnects. WebSockets require authentication and room membership where applicable, allow at most 20 connections/account and 60/IP in five minutes per socket type, and accept only JSON heartbeat messages up to 4 KiB; clients cannot publish game outcomes. These signals are indicators for human review, not proof of abuse: shared households, workplaces, schools, carrier NAT, VPNs, and common browser versions can create legitimate overlap. The application does not automatically accuse, suspend, or ban accounts based on these signals.

IP attribution uses the direct connection peer address to avoid trusting spoofable forwarded headers. Behind a reverse proxy, multiple players may be grouped under the proxy address; configure trusted proxy handling before relying on IP limits or interpreting shared-IP signals. A user-agent is a weak, client-controlled browser hint, not a device identity.

### Credential revocation

- **Django signing key:** Generate a new `DJANGO_SECRET_KEY`, update the deployment secret store, and restart all application instances. Treat the old key as compromised; replacing it invalidates data signed with that key, including existing password-reset links.
- **Opaypesa API key:** Revoke the exposed key in the Opaypesa merchant portal, create a replacement key for the same environment, update `OPAYPESA_API_KEY` in the secret store, and restart application instances.
- **ioTec Pay API credentials:** Revoke or rotate the client credentials with the provider, update `IOTEC_PAY_CLIENT_ID` and `IOTEC_PAY_CLIENT_SECRET` in the secret store, then restart application instances. Contact ioTec if provider-side transactions may have been affected.
- **ioTec Pay callback token:** Generate a new high-entropy `IOTEC_PAY_CALLBACK_TOKEN`, update the app and ioTec callback configuration together, and confirm callbacks with the new token succeed. Until both ends match, callbacks are rejected; use the authenticated payment status-sync flow to reconcile pending requests.
- **Email credentials:** Revoke the SMTP credential at the email provider, replace `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD`, and verify a test alert reaches the configured `DJANGO_ADMINS`.
- **Database credentials:** Rotate the database user credential with the database provider, update `DATABASE_URL`, restart all instances, and review database access logs.
- **Compromised user or operator account:** Use the custom staff console to lock or revoke access, then reset credentials through account recovery when a verified email is available or through a trusted server-side operation. To invalidate every active web session after a wider compromise, run `python manage.py shell -c "from django.contrib.sessions.models import Session; Session.objects.all().delete()"`.

### Incident response checklist

1. Acknowledge the alert and preserve the relevant audit records and restricted host/provider logs before making changes. Record the time, affected accounts, source addresses, and actions taken; do not copy passwords, tokens, or full payment details into an incident ticket.
2. Confirm whether the configuration change was authorized. If not, restore known-good security settings, revoke and rotate the affected credentials, restart all application instances, and invalidate active sessions if account or signing-key access may be compromised.
3. For repeated login failures, inspect the security audit events, check for successful logins and sensitive account activity around the same time, disable or reset affected accounts as appropriate, and notify impacted users when required.
4. For suspected payment abuse, contact the active payment provider, pause affected credentials if needed, reconcile pending and completed transactions using provider records, and do not credit a wallet based solely on an unverified notification.
5. Verify that `DEBUG=False`, production hosts and HTTPS settings are correct, and test security alerts and payment status reconciliation. Document the root cause, recovery, and any follow-up controls.

Local development continues to use the existing SQLite database unless `DATABASE_URL` is set. To prepare for PostgreSQL, configure `DATABASE_URL` with the private connection URL supplied by your database provider. PostgreSQL support and its driver are included, but SQLite remains the default until that variable is explicitly configured.

## Setup

1. Create a Python virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Create the local `.env` file as described above.

4. Apply migrations:

```powershell
python manage.py migrate
```

5. Bootstrap the initial superuser from a trusted development or deployment environment:

```powershell
python manage.py createsuperuser
```

The custom staff console uses phone-number sign-in. Associate a normalized phone number with this initial account's `Profile` before using `/admin-analytics/`:

```powershell
python manage.py shell
```

```python
from django.contrib.auth.models import User
from core.models import Profile

admin_user = User.objects.get(username="<username-created-in-step-5>")
Profile.objects.update_or_create(
    user=admin_user,
    defaults={"phone_number": "2567XXXXXXXX"},
)
```

Replace the example phone with the account's normalized country-code number. Subsequent administrators can be provisioned from the Admin team page.

6. Start the development server:

```powershell
python manage.py runserver
```

7. Open `http://127.0.0.1:8000` in your browser.

## Usage

- **Home**: `http://127.0.0.1:8000/`
- **Register**: `http://127.0.0.1:8000/register/`
- **Login**: `http://127.0.0.1:8000/login/`
- **Play Game**: `http://127.0.0.1:8000/play/` (after login)
- **Legacy ioTec Callback**: `http://127.0.0.1:8000/api/payments/callback/` (Opaypesa uses status polling)
- **Staff Operations Console**: `http://127.0.0.1:8000/admin-analytics/` (custom staff-only console; sign in with the phone number on the staff account's profile)
- **Account management**: `/admin-analytics/users/` (lock, ban, revoke sessions, or anonymize player identity while retaining financial/game records)
- **Round players and wallet refunds**: `/admin-analytics/players/` (view bet activity for a selected round; eligible lost-bet refunds credit the internal wallet only)
- **Platform settings**: `/admin-analytics/settings/` (edit branding and set the shared-round maximum between 250x and 286x; changes apply to new rounds)
- **Administrator access**: `/admin-analytics/admins/` (staff may create operations accounts; only an existing superuser may grant or revoke superuser access)
- **Security events**: `/admin-analytics/security/`

## Production

Set the following values in your deployment environment or private production `.env`:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG=False`
- `DJANGO_ALLOWED_HOSTS=yourdomain.com`
- `DJANGO_SECURE_SSL_REDIRECT=True`
- `DATABASE_URL=postgresql://...`
- `PAYMENT_GATEWAY=opaypesa`
- `OPAYPESA_MODE=live`
- `OPAYPESA_API_KEY`

Configure and test PostgreSQL before switching production traffic; local development remains on SQLite when `DATABASE_URL` is empty. Live Opaypesa mode requires `DJANGO_DEBUG=False` and an `opaypesa_live_…` API key. Then collect static files:

```powershell
python manage.py collectstatic
```

### Queued withdrawals

The withdrawal worker is for ioTec Pay requests only. Opaypesa withdrawals are submitted directly to its disbursement endpoint and can be refreshed from the wallet using the returned transaction ID.

Schedule `python manage.py process_queued_withdrawals --batch-size 20` to run every minute (or at your chosen operational cadence). Configure the scheduler to prevent overlapping invocations on a host; the database lock also serializes workers across hosts. Monitor command failures and queued requests in the staff operations console. Do not manually resubmit queued requests or clear their holds; use the documented provider-reconciliation procedure for any payment exception.
