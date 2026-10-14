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
from .constants import MAXIMUM_DEPOSIT_AMOUNT, MINIMUM_MOBILE_MONEY_AMOUNT
from .models import ChatMessage, PaymentRequest, Profile, Room
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
    email = forms.EmailField(required=True, widget=forms.EmailInput(attrs={
        'placeholder': 'name@example.com',
        'autocomplete': 'email',
    }))
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
        fields = ['username', 'email', 'password1', 'password2']

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        if commit:
            user.save()
        return user

    def clean_phone_number(self):
        phone_number = super().clean_phone_number()
        if Profile.objects.filter(phone_number=phone_number).exists():
            raise forms.ValidationError('An account with this phone number already exists.')
        return phone_number

    def clean_email(self):
        email = self.cleaned_data['email'].strip()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email address already exists.')
        return email


class PhoneOrUsernameAuthenticationForm(AuthenticationForm):
    username = forms.CharField(
        label='Username or phone number',
        widget=forms.TextInput(attrs={
            'placeholder': 'Username or 07XXXXXXXX',
            'autocomplete': 'username',
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
        identifier = self.cleaned_data.get('username', '').strip()
        if identifier:
            try:
                phone_number = normalize_mobile_money_phone(identifier)
            except ValueError:
                phone_number = ''
            if phone_number:
                profile = Profile.objects.select_related('user').filter(phone_number=phone_number).first()
                if profile:
                    self.cleaned_data['username'] = profile.user.get_username()
            else:
                self.cleaned_data['username'] = identifier
        return super().clean()


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
            'placeholder': '500 - 300,000 UGX',
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
        min_value=1,
        max_value=MAXIMUM_DEPOSIT_AMOUNT,
        widget=forms.NumberInput(attrs={
            'placeholder': 'Maximum 300,000 UGX',
            'class': 'form-control',
            'min': '1',
            'max': str(MAXIMUM_DEPOSIT_AMOUNT),
            'step': '0.01',
        })
    )
    daily_loss_limit = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=1,
        widget=forms.NumberInput(attrs={'placeholder': 'Daily loss limit', 'class': 'form-control'})
    )

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

    report_type = forms.ChoiceField(choices=REPORT_TYPES, widget=forms.Select(attrs={'class': 'form-control'}))
    period_start = forms.DateField(widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}))
    period_end = forms.DateField(widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}))


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
