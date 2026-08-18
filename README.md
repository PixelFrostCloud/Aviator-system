# Pakabet (Django)

Professional Django web application built with Django, HTML, CSS, JavaScript, and ioTec Pay wallet funding.

## Features

- **User Authentication**: Functional registration and login system with automatic redirects to the game
- **Pakabet Crash Game**: Interactive betting interface with real-time multiplier animation and cash-out functionality
- **Wallet Management**: ioTec Pay collections, disbursements, callback settlement, and transaction history
- **Admin Analytics Dashboard**: Comprehensive platform monitoring and user statistics
- **Multiplayer Rooms**: Real-time room support with WebSocket synchronization
- **Audit Logging**: Complete system activity tracking with admin reporting interface
- **Responsive Design**: Clean templates that work across all devices

## Payment Integration

The app reads the ioTec Pay OpenAPI files from `Payment APIs/` and integrates the main production rails:

- Mobile money collections: `/api/collections/collect`
- Card collections: `/api/collections/collect/card`
- Mobile money disbursements: `/api/disbursements/disburse`
- Status lookup by external ID
- Provider callbacks at `/api/payments/callback/`

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
