from django.db import migrations, models


ABUSIVE_TERMS = (
    'Idiot', 'Stupid', 'Fool', 'Moron', 'Imbecile', 'Jerk', 'Loser', 'Dummy',
    'Clown', 'Nitwit', 'Blockhead', 'Airhead', 'Dunce', 'Bonehead', 'Dimwit',
    'Buffoon', 'Simpleton', 'Doofus', 'Foolish', 'Incompetent', 'Fuck', 'Shit',
    'Damn', 'Hell', 'Ass', 'Asshole', 'Bitch', 'Bastard', 'Bullshit', 'Crap',
    'Dick', 'Piss', 'Prick', 'Motherfucker', 'Son of a bitch', 'Jackass',
    'Dumbass', 'Dipshit', 'Shitty', 'Fucking', 'Pathetic', 'Useless',
    'Worthless', 'Disgusting', 'Coward', 'Cheat', 'Liar', 'Traitor', 'Hypocrite',
    'Selfish', 'Arrogant', 'Shameless', 'Heartless', 'Spineless', 'Despicable',
    'Vile', 'Contemptible', 'Repulsive', 'Miserable', 'Shut up!', 'Get lost!',
    'Go to hell!', 'Leave me alone!', 'You disgust me!', 'What is wrong with you?',
    "You're pathetic!", "You're a disgrace!", 'Nobody likes you!',
    "You're good for nothing!", 'Mind your own business!', 'Get out of my face!',
    "You're a complete joke!", 'You make me sick!', "Don't mess with me!",
    'Troll', 'Hater', 'Toxic', 'Creep', 'Weirdo', 'Freak', 'Psycho', 'Crazed',
    'Stalker', 'Bully', 'Fraud', 'Fake', 'Scammer', 'Attention seeker', 'Slut',
    'Whore', 'Pervert', 'Pimp', 'Skank', 'Horny bastard',
    'arse', 'arsehole', 'asshat', 'bollocks', 'cock', 'cunt', 'dickhead',
    'douche', 'douchebag', 'dumb', 'fucker', 'fuckface', 'fuckoff', 'fuckyou',
    'scumbag', 'shithead', 'silly', 'twat', 'wanker',
)


def seed_abusive_terms(apps, schema_editor):
    term_model = apps.get_model('core', 'ChatModerationTerm')
    existing_terms = {term.casefold() for term in term_model.objects.values_list('term', flat=True)}
    new_terms = []
    for term in ABUSIVE_TERMS:
        normalized_term = term.casefold()
        if normalized_term not in existing_terms:
            new_terms.append(term_model(term=term, is_active=True))
            existing_terms.add(normalized_term)
    term_model.objects.bulk_create(new_terms)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_alter_room_crash_point_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='ChatModerationTerm',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('term', models.CharField(max_length=120, unique=True)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['term'],
            },
        ),
        migrations.RunPython(seed_abusive_terms, migrations.RunPython.noop),
    ]
