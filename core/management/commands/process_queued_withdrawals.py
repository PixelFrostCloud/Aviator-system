from django.core.management.base import BaseCommand, CommandError

from core.payment_services import process_next_queued_withdrawal


class Command(BaseCommand):
    help = 'Submit queued withdrawals when the payment provider has sufficient available funds.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size',
            type=int,
            default=20,
            help='Maximum number of withdrawals to process in one invocation.',
        )

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        if batch_size < 1:
            raise CommandError('--batch-size must be at least 1.')

        submitted = 0
        outcome = 'empty'
        for _ in range(batch_size):
            outcome = process_next_queued_withdrawal()
            if outcome != 'submitted':
                break
            submitted += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'Submitted {submitted} queued withdrawal(s); processor outcome: {outcome}.'
            )
        )
