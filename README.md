# Pakabet (Django)

Professional Django web application built with Django, HTML, CSS, JavaScript, and ioTec Pay wallet funding.

## Features

- **User Authentication**: Registration and login plus one-time, expiring email password-reset links
- **Pakabet Crash Game**: Interactive betting interface with real-time multiplier animation and cash-out functionality
- **Wallet Management**: ioTec Pay collections, disbursements, callback settlement, and transaction history
- **Referral Programme**: Personal invite links automatically attribute new registrations; inviters receive a platform-funded, withdrawable 5% of each referred player's first successful deposit
- **Admin Analytics Dashboard**: Comprehensive platform monitoring and user statistics
- **Multiplayer Rooms**: Real-time room support with WebSocket synchronization
- **Audit Logging**: Complete system activity tracking with admin reporting interface
- **Responsive Design**: Clean templates that work across all devices

## Payment Integration

The app reads the ioTec Pay OpenAPI files from `Payment APIs/` and integrates the main production rails:

- Mobile money collections: `/api/collections/collect`
- Mobile money disbursements: `/api/disbursements/disburse`
- Status lookup by external ID
- Provider callbacks at `/api/payments/callback/`

The wallet accepts only MTN Mobile Money and Airtel Money in Uganda. Deposits must be between 500 and 300,000 UGX per transaction. Withdrawals start at 500 UGX and are limited by the player's available balance. Self-exclusion blocks deposits and new bets for the selected period.

In live mode, a deposit calls ioTec's mobile-money collection endpoint and the customer receives the network prompt on the submitted phone. The wallet is credited only after ioTec reports `Success` through the callback or a status sync. Withdrawals create a mobile-money disbursement and hold the balance until the provider completes or fails it; failed withdrawals automatically refund the hold. Sandbox mode does not send a real phone prompt.

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

Players can set or securely generate their client seed in the in-game Provably Fair settings. The saved seed is used for future rounds only; every round stores its own client seed and server-seed hash before play. The server seed is revealed after the round ends, and the Provably Fair page independently checks the commitment hash and crash-point calculation for each of the player's rounds.

Local development defaults to `IOTEC_PAY_MODE=sandbox`, so the app runs without live credentials. For live payments set:

```powershell
$env:IOTEC_PAY_MODE='live'
$env:IOTEC_PAY_CLIENT_ID='your-client-id'
$env:IOTEC_PAY_CLIENT_SECRET='your-client-secret'
$env:IOTEC_PAY_WALLET_ID='your-wallet-id'
$env:IOTEC_PAY_CALLBACK_TOKEN='shared-callback-token'
```

## Environment configuration

Copy the example configuration into a private local environment file:

```powershell
Copy-Item .env.example .env
```

Generate a unique key and replace the example value of `DJANGO_SECRET_KEY` in `.env` before starting the app:

```powershell
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Keep `.env` private; it is excluded from Git. Put email credentials and ioTec Pay API credentials in `.env` rather than source code or shell history.

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
- **ioTec Pay API credentials:** Revoke or rotate the client credentials with the provider, update `IOTEC_PAY_CLIENT_ID` and `IOTEC_PAY_CLIENT_SECRET` in the secret store, then restart application instances. Contact ioTec if provider-side transactions may have been affected.
- **Payment callback token:** Generate a new high-entropy `IOTEC_PAY_CALLBACK_TOKEN`, update the app and the provider callback configuration together, and confirm callbacks with the new token succeed. Until both ends match, callbacks are rejected; use the authenticated payment status-sync flow to reconcile pending requests.
- **Email credentials:** Revoke the SMTP credential at the email provider, replace `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD`, and verify a test alert reaches the configured `DJANGO_ADMINS`.
- **Database credentials:** Rotate the database user credential with the database provider, update `DATABASE_URL`, restart all instances, and review database access logs.
- **Compromised user or operator account:** Set the account inactive or reset its password from Django Admin. To invalidate every active web session after an operator compromise, run `python manage.py shell -c "from django.contrib.sessions.models import Session; Session.objects.all().delete()"`.

### Incident response checklist

1. Acknowledge the alert and preserve the relevant audit records and restricted host/provider logs before making changes. Record the time, affected accounts, source addresses, and actions taken; do not copy passwords, tokens, or full payment details into an incident ticket.
2. Confirm whether the configuration change was authorized. If not, restore known-good security settings, revoke and rotate the affected credentials, restart all application instances, and invalidate active sessions if account or signing-key access may be compromised.
3. For repeated login failures, inspect the security audit events, check for successful logins and sensitive account activity around the same time, disable or reset affected accounts as appropriate, and notify impacted users when required.
4. For suspected payment abuse, contact ioTec Pay, pause affected payment credentials or callback delivery if needed, reconcile pending and completed transactions using provider records, and do not credit a wallet based solely on an unverified notification.
5. Verify that `DEBUG=False`, production hosts and HTTPS settings are correct, callback authentication is configured, and test security alerts and payment reconciliation. Document the root cause, recovery, and any follow-up controls.

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

5. Create admin user:

```powershell
python manage.py createsuperuser
```

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
- **Payment Callback**: `http://127.0.0.1:8000/api/payments/callback/`
- **Admin Panel**: `http://127.0.0.1:8000/admin/`
- **Audit Logs**: `http://127.0.0.1:8000/audit-logs/` (admin only)

## Production

Set the following values in your deployment environment or private production `.env`:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG=False`
- `DJANGO_ALLOWED_HOSTS=yourdomain.com`
- `DJANGO_SECURE_SSL_REDIRECT=True`
- `DATABASE_URL=postgresql://...`
- `IOTEC_PAY_MODE=live`
- `IOTEC_PAY_CLIENT_ID`
- `IOTEC_PAY_CLIENT_SECRET`
- `IOTEC_PAY_WALLET_ID`
- `IOTEC_PAY_CALLBACK_TOKEN`

Configure and test PostgreSQL before switching production traffic; local development remains on SQLite when `DATABASE_URL` is empty. Then collect static files:

```powershell
python manage.py collectstatic
```

### Queued withdrawals

In live mode, withdrawal requests are recorded with a unique provider reference and the player's funds are held before payout submission. A scheduled worker checks ioTec Pay's `availableBalance` and submits queued payouts only when sufficient funds are confirmed; it reuses the original external reference for idempotent provider processing. Unavailable or invalid balance data leaves the withdrawal queued and held.

Schedule `python manage.py process_queued_withdrawals --batch-size 20` to run every minute (or at your chosen operational cadence). Configure the scheduler to prevent overlapping invocations on a host; the database lock also serializes workers across hosts. Monitor command failures and the queued-withdrawal count in Django admin. Do not manually resubmit queued requests or clear their holds; use the admin failure action only when an operator has confirmed that no payout was submitted.
