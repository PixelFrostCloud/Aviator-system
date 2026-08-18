from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import ChatMessage, KYCSubmission, PaymentRequest, Room


class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=True, widget=forms.EmailInput(attrs={'placeholder': 'Email'}))

    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2']

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        if commit:
            user.save()
        return user


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


class WalletForm(forms.Form):
    PROVIDER_CHOICES = [
        ('mtn_momo', 'MTN Mobile Money'),
        ('airtel_money', 'Airtel Money'),
        ('card', 'Visa/Mastercard via PegPay'),
    ]

    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=1,
        widget=forms.NumberInput(attrs={
            'placeholder': 'Amount in UGX',
            'class': 'form-control',
        })
    )
    provider = forms.ChoiceField(
        choices=PROVIDER_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    phone_number = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={
            'placeholder': 'Phone number for MoMo/Airtel',
            'class': 'form-control',
        })
    )

    def clean(self):
        cleaned_data = super().clean()
        provider = cleaned_data.get('provider')
        phone_number = cleaned_data.get('phone_number', '').strip()
        if provider in ['mtn_momo', 'airtel_money'] and not phone_number:
            self.add_error('phone_number', 'Phone number is required for mobile money payments.')
        if provider == 'card' and self.prefix == 'withdraw':
            self.add_error('provider', 'Card withdrawals are not supported by this wallet flow.')
        return cleaned_data


class ProfileComplianceForm(forms.Form):
    phone_number = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={'placeholder': 'Phone number', 'class': 'form-control'})
    )
    daily_deposit_limit = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=1,
        widget=forms.NumberInput(attrs={'placeholder': 'Daily deposit limit', 'class': 'form-control'})
    )
    daily_loss_limit = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=1,
        widget=forms.NumberInput(attrs={'placeholder': 'Daily loss limit', 'class': 'form-control'})
    )


class KYCSubmissionForm(forms.ModelForm):
    class Meta:
        model = KYCSubmission
        fields = ['document_type', 'document_number', 'full_name', 'date_of_birth']
        widgets = {
            'document_type': forms.Select(attrs={'class': 'form-control'}),
            'document_number': forms.TextInput(attrs={'placeholder': 'Document number', 'class': 'form-control'}),
            'full_name': forms.TextInput(attrs={'placeholder': 'Full legal name', 'class': 'form-control'}),
            'date_of_birth': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
        }


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
            'message': forms.TextInput(attrs={'placeholder': 'Your message...', 'class': 'form-control', 'maxlength': 160}),
        }


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
