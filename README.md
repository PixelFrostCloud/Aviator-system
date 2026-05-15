# Aviator System (Django)

Professional Django web application scaffold built with Django, HTML, CSS, and JavaScript.

## Features

- **User Authentication**: Functional registration and login system with automatic redirects to the game
- **Aviator Game**: Interactive betting interface with real-time multiplier animation and cash-out functionality
- **Wallet Management**: Deposit and withdrawal system with transaction history
- **Admin Analytics Dashboard**: Comprehensive platform monitoring and user statistics
- **Multiplayer Rooms**: Real-time room support with WebSocket synchronization
- **Audit Logging**: Complete system activity tracking with admin reporting interface
- **Responsive Design**: Clean templates that work across all devices

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
- **Admin Panel**: `http://127.0.0.1:8000/admin/`
- **Audit Logs**: `http://127.0.0.1:8000/audit-logs/` (admin only)

## Production

Set the following environment variables for production:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG=False`
- `DJANGO_ALLOWED_HOSTS=yourdomain.com`
- `DJANGO_SECURE_SSL_REDIRECT=True`

Then collect static files:

```powershell
python manage.py collectstatic
```
