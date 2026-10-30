from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm as DjangoPasswordChangeForm,
    PasswordResetForm as DjangoPasswordResetForm,
    SetPasswordForm as DjangoSetPasswordForm,
    UserCreationForm,
)
from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator
from .constants import (
    MAXIMUM_DAILY_DEPOSIT_LIMIT,
    MAXIMUM_DEPOSIT_AMOUNT,
    MINIMUM_DAILY_DEPOSIT_LIMIT,
    MINIMUM_MOBILE_MONEY_AMOUNT,
)
from .models import ChatMessage, PaymentRequest, PlatformSettings, Profile, Room
from .chat_rules import get_chat_rule_violation
from .payment_services import (
    MOBILE_MONEY_COUNTRY_CHOICES,
    mobile_money_country_availability_error,
    MOBILE_MONEY_PROVIDERS,
    normalize_mobile_money_phone,
    split_mobile_money_phone,
)


class PhoneNumberFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        initial_phone = self.initial.get('phone_number')
        if initial_phone:
            country_code, national_number = split_mobile_money_phone(initial_phone)
            self.initial['country_code'] = country_code
            self.initial['phone_number'] = national_number

    def clean_phone_number(self):
        phone_number = self.cleaned_data.get('phone_number', '').strip()
        if not phone_number:
            return ''
        try:
            return normalize_mobile_money_phone(
                phone_number,
                self.cleaned_data.get('country_code') or '256',
            )
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc


class RegisterForm(PhoneNumberFormMixin, UserCreationForm):
    country_code = forms.ChoiceField(
        label='Country code',
        choices=MOBILE_MONEY_COUNTRY_CHOICES,
        required=False,
        initial='256',
        widget=forms.Select(attrs={'class': 'country-code-select', 'autocomplete': 'tel-country-code'}),
    )
    phone_number = forms.CharField(
        label='Phone number',
        required=True,
        max_length=20,
        widget=forms.TextInput(attrs={
            'placeholder': '7XXXXXXXX',
            'autocomplete': 'tel',
            'inputmode': 'tel',
        }),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].widget.attrs.update({
            'placeholder': 'Choose a username',
            'autocomplete': 'username',
        })
        self.fields['password1'].widget.attrs.update({
            'placeholder': 'Create a password',
            'autocomplete': 'new-password',
        })
        self.fields['password2'].widget.attrs.update({
            'placeholder': 'Confirm your password',
            'autocomplete': 'new-password',
        })

    class Meta:
        model = User
        fields = ['username', 'password1', 'password2']

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = ''
        if commit:
            user.save()
        return user

    def clean_phone_number(self):
        phone_number = super().clean_phone_number()
        if Profile.objects.filter(phone_number=phone_number).exists():
            raise forms.ValidationError('An account with this phone number already exists.')
        return phone_number


class PhoneNumberAuthenticationForm(AuthenticationForm):
    username = forms.CharField(
        label='Phone number',
        widget=forms.TextInput(attrs={
            'placeholder': '07XXXXXXXX',
            'autocomplete': 'tel',
            'inputmode': 'tel',
            'autofocus': True,
        }),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['password'].widget.attrs.update({
            'placeholder': 'Enter your password',
            'autocomplete': 'current-password',
        })

    def clean(self):
        phone_number = self.cleaned_data.get('username', '').strip()
        try:
            phone_number = normalize_mobile_money_phone(phone_number)
        except ValueError:
            phone_number = ''

        profiles = Profile.objects.select_related('user').filter(
            phone_number=phone_number,
        ).order_by('pk')[:2] if phone_number else []
        if len(profiles) == 1 and profiles[0].account_status == 'active':
            self.cleaned_data['username'] = profiles[0].user.get_username()
        else:
            self.cleaned_data['username'] = 'invalid-phone-number'
        return super().clean()


class PlatformBrandingForm(forms.ModelForm):
    class Meta:
        model = PlatformSettings
        fields = ['site_name', 'site_tagline', 'logo', 'favicon']
        widgets = {
            'site_name': forms.TextInput(attrs={'placeholder': 'Your platform name'}),
            'site_tagline': forms.TextInput(attrs={'placeholder': 'A short description shown with your brand'}),
            'logo': forms.ClearableFileInput(attrs={'accept': '.png,.jpg,.jpeg,.webp'}),
            'favicon': forms.ClearableFileInput(attrs={'accept': '.png,.jpg,.jpeg,.webp,.ico'}),
        }


class PlatformGameSettingsForm(forms.ModelForm):
    class Meta:
        model = PlatformSettings
        fields = ['maximum_crash_point', 'betting_seconds']
        widgets = {
            'maximum_crash_point': forms.NumberInput(attrs={
                'placeholder': '250.00 to 286.00',
                'min': '250.00',
                'max': '286.00',
                'step': '0.01',
            }),
            'betting_seconds': forms.NumberInput(attrs={
                'placeholder': '2 to 15 seconds',
                'min': '2',
                'max': '15',
                'step': '1',
            }),
        }


class AdminAccountCreationForm(forms.Form):
    ROLE_CHOICES = (
        ('staff', 'Operations administrator'),
        ('superuser', 'Super administrator'),
    )
    username = forms.CharField(
        max_length=150,
        validators=User._meta.get_field('username').validators,
        widget=forms.TextInput(attrs={
            'placeholder': 'Choose a username',
            'autocomplete': 'username',
        }),
    )
    phone_number = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={
            'placeholder': '07XXXXXXXX',
            'autocomplete': 'tel',
            'inputmode': 'tel',
        }),
    )
    role = forms.ChoiceField(choices=ROLE_CHOICES)
    password1 = forms.CharField(
        label='Temporary password',
        widget=forms.PasswordInput(attrs={
            'placeholder': 'Create a strong temporary password',
            'autocomplete': 'new-password',
        }),
    )
    password2 = forms.CharField(
        label='Confirm password',
        widget=forms.PasswordInput(attrs={
            'placeholder': 'Repeat the temporary password',
            'autocomplete': 'new-password',
        }),
    )

    def __init__(self, *args, allow_superuser=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not allow_superuser:
            self.fields['role'].choices = (('staff', 'Operations administrator'),)
        self.fields['role'].widget.attrs.update({'aria-label': 'Administrator role'})

    def clean_phone_number(self):
        try:
            phone_number = normalize_mobile_money_phone(self.cleaned_data['phone_number'])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        if Profile.objects.filter(phone_number=phone_number).exists():
            raise forms.ValidationError('An account already uses this phone number.')
        return phone_number

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError('An account with this username already exists.')
        return username

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get('password1') != cleaned_data.get('password2'):
            self.add_error('password2', 'The passwords do not match.')
        from django.core.exceptions import ValidationError
        from django.contrib.auth.password_validation import validate_password
        try:
            validate_password(cleaned_data.get('password1', ''))
        except ValidationError as exc:
            self.add_error('password1', exc)
        return cleaned_data


class AccountPasswordChangeForm(DjangoPasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['old_password'].widget.attrs.update({
            'placeholder': 'Enter your current password',
            'autocomplete': 'current-password',
        })
        self.fields['new_password1'].widget.attrs.update({
            'placeholder': 'Choose a new password',
            'autocomplete': 'new-password',
        })
        self.fields['new_password2'].widget.attrs.update({
            'placeholder': 'Enter your new password again',
            'autocomplete': 'new-password',
        })


class AccountPasswordResetForm(DjangoPasswordResetForm):
    email = forms.EmailField(
        label='Email address',
        widget=forms.EmailInput(attrs={
            'placeholder': 'Enter the email on your account',
            'autocomplete': 'email',
            'autofocus': True,
        }),
    )

    def get_users(self, email):
        users = list(
            User._default_manager.filter(email__iexact=email, is_active=True)
            .order_by('pk')[:2]
        )
        if len(users) != 1 or not users[0].has_usable_password():
            return
        yield users[0]


class AccountSetPasswordForm(DjangoSetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['new_password1'].widget.attrs.update({
            'placeholder': 'Choose a new password',
            'autocomplete': 'new-password',
            'autofocus': True,
        })
        self.fields['new_password2'].widget.attrs.update({
            'placeholder': 'Enter your new password again',
            'autocomplete': 'new-password',
        })


class PlayForm(forms.Form):
    stake = forms.DecimalField(
        max_digits=8,
        decimal_places=2,
        min_value=1,
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'placeholder': 'Stake amount',
        })
    )


class WalletForm(PhoneNumberFormMixin, forms.Form):
    PROVIDER_CHOICES = [
        ('mtn_momo', 'MTN Mobile Money'),
        ('airtel_money', 'Airtel Money'),
    ]

    amount = forms.DecimalField(
        label='Amount (UGX)',
        max_digits=12,
        decimal_places=2,
        min_value=MINIMUM_MOBILE_MONEY_AMOUNT,
        max_value=MAXIMUM_DEPOSIT_AMOUNT,
        widget=forms.NumberInput(attrs={
            'placeholder': '500 - 10,000,000 UGX',
            'class': 'form-control',
            'min': str(MINIMUM_MOBILE_MONEY_AMOUNT),
            'max': str(MAXIMUM_DEPOSIT_AMOUNT),
            'step': '0.01',
            'inputmode': 'decimal',
            'autocomplete': 'off',
        })
    )
    provider = forms.ChoiceField(
        label='Mobile Money network',
        choices=PROVIDER_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    country_code = forms.ChoiceField(
        label='Country code',
        choices=MOBILE_MONEY_COUNTRY_CHOICES,
        required=False,
        initial='256',
        widget=forms.Select(attrs={'class': 'form-control country-code-select', 'autocomplete': 'tel-country-code'}),
    )
    phone_number = forms.CharField(
        label='Mobile Money number',
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={
            'placeholder': '7XXXXXXXX',
            'class': 'form-control',
            'autocomplete': 'tel',
            'inputmode': 'tel',
        })
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.prefix == 'withdraw':
            amount_field = self.fields['amount']
            amount_field.max_value = None
            amount_field.validators = [
                validator for validator in amount_field.validators
                if not isinstance(validator, MaxValueValidator)
            ]
            amount_field.widget.attrs.pop('max', None)
            amount_field.widget.attrs['placeholder'] = 'Minimum 500 UGX'
            self.fields['phone_number'].disabled = True
            self.fields['country_code'].disabled = True

    def clean(self):
        cleaned_data = super().clean()
        provider = cleaned_data.get('provider')
        phone_number = cleaned_data.get('phone_number', '').strip()
        if provider not in MOBILE_MONEY_PROVIDERS:
            self.add_error('provider', 'Only MTN Mobile Money and Airtel Money are supported.')
        elif not phone_number:
            self.add_error('phone_number', 'Phone number is required for mobile money payments.')
        else:
            availability_error = mobile_money_country_availability_error(cleaned_data.get('country_code'))
            if availability_error:
                self.add_error('country_code', availability_error)
        return cleaned_data


class ProfileComplianceForm(PhoneNumberFormMixin, forms.Form):
    country_code = forms.ChoiceField(
        label='Country code',
        choices=MOBILE_MONEY_COUNTRY_CHOICES,
        required=False,
        initial='256',
        widget=forms.Select(attrs={'class': 'form-control country-code-select', 'autocomplete': 'tel-country-code'}),
    )
    phone_number = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={'placeholder': 'Phone number', 'class': 'form-control'})
    )
    daily_deposit_limit = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=MINIMUM_DAILY_DEPOSIT_LIMIT,
        max_value=MAXIMUM_DAILY_DEPOSIT_LIMIT,
        widget=forms.NumberInput(attrs={
            'placeholder': '500 - 10,000,000 UGX',
            'class': 'form-control',
            'min': str(MINIMUM_DAILY_DEPOSIT_LIMIT),
            'max': str(MAXIMUM_DAILY_DEPOSIT_LIMIT),
            'step': '0.01',
        })
    )
    daily_loss_limit = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=1,
        widget=forms.NumberInput(attrs={'placeholder': 'Daily loss limit', 'class': 'form-control'})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['country_code'].disabled = True
        self.fields['phone_number'].disabled = True

class SelfExclusionForm(forms.Form):
    DURATION_CHOICES = [
        ('72', '72 hours'),
        ('168', '7 days'),
        ('720', '30 days'),
        ('permanent', 'Permanent'),
    ]

    duration = forms.ChoiceField(choices=DURATION_CHOICES, widget=forms.Select(attrs={'class': 'form-control'}))
    reason = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={'placeholder': 'Reason optional', 'class': 'form-control'})
    )


class ChatMessageForm(forms.ModelForm):
    class Meta:
        model = ChatMessage
        fields = ['message']
        widgets = {
            'message': forms.TextInput(attrs={
                'placeholder': 'Your message...',
                'class': 'form-control',
                'maxlength': 160,
            }),
        }

    def clean_message(self):
        message = self.cleaned_data['message']
        violation = get_chat_rule_violation(message)
        if violation:
            raise forms.ValidationError(violation)
        return message


class FinancialReportForm(forms.Form):
    REPORT_TYPES = [
        ('daily', 'Daily'),
        ('monthly', 'Monthly'),
    ]

    report_type = forms.ChoiceField(
        choices=REPORT_TYPES,
        widget=forms.Select(attrs={'class': 'form-control', 'aria-label': 'Select report frequency'}),
    )
    period_start = forms.DateField(widget=forms.DateInput(attrs={
        'type': 'date',
        'class': 'form-control',
        'placeholder': 'Select a start date',
    }))
    period_end = forms.DateField(widget=forms.DateInput(attrs={
        'type': 'date',
        'class': 'form-control',
        'placeholder': 'Select an end date',
    }))


class RoomCreateForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = ['name', 'max_players']
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'Room name'}),
            'max_players': forms.NumberInput(attrs={'min': 2, 'max': 12}),
        }


class RoomJoinForm(forms.Form):
    stake = forms.DecimalField(
        max_digits=8,
        decimal_places=2,
        min_value=1,
        widget=forms.NumberInput(attrs={
            'placeholder': 'Stake amount',
            'class': 'form-control',
        })
    )
