# Pakabet (Django)

Professional Django web application built with Django, HTML, CSS, JavaScript, and ioTec Pay wallet funding.

## Features

- **User Authentication**: Functional registration and login system with automatic redirects to the game
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

3. Create and apply migrations:

```powershell
python manage.py makemigrations
python manage.py migrate
```

4. Start the development server:

```powershell
python manage.py migrate
```

4. Create admin user:

```powershell
python manage.py createsuperuser
```

5. Start the development server:

```powershell
python manage.py runserver
```

6. Open `http://127.0.0.1:8000` in your browser.

## Usage

- **Home**: `http://127.0.0.1:8000/`
- **Register**: `http://127.0.0.1:8000/register/`
- **Login**: `http://127.0.0.1:8000/login/`
- **Play Game**: `http://127.0.0.1:8000/play/` (after login)
- **Payment Callback**: `http://127.0.0.1:8000/api/payments/callback/`
- **Admin Panel**: `http://127.0.0.1:8000/admin/`
- **Audit Logs**: `http://127.0.0.1:8000/audit-logs/` (admin only)

## Production

Set the following environment variables for production:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG=False`
- `DJANGO_ALLOWED_HOSTS=yourdomain.com`
- `DJANGO_SECURE_SSL_REDIRECT=True`
- `IOTEC_PAY_MODE=live`
- `IOTEC_PAY_CLIENT_ID`
- `IOTEC_PAY_CLIENT_SECRET`
- `IOTEC_PAY_WALLET_ID`
- `IOTEC_PAY_CALLBACK_TOKEN`

Then collect static files:

```powershell
python manage.py collectstatic
```
