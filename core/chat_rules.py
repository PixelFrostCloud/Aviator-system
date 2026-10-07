import re
import unicodedata


_PHONE_DIGITS = re.compile(r'(?<!\w)\+?\d(?:[\s()./\-–—]*\d){6,}(?!\w)')
_NUMBER_WORD = (
    r'zero|oh|o|nought|nil|one|two|three|four|five|six|seven|eight|nine|ten|'
    r'eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|'
    r'nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|'
    r'hundred|thousand|and|double|triple'
)
_PHONE_WORDS = re.compile(
    rf'\b(?:{_NUMBER_WORD})\b(?:[\s,./\-–—;:|]+\b(?:{_NUMBER_WORD})\b)*',
    re.IGNORECASE,
)
_LINK = re.compile(
    r'(?:https?://|hxxps?://|ftp://|www\.)\S+|'
    r'\b(?:[\w-]+\.)+[a-z]{2,}\b(?:/\S*)?|'
    r'\b[\w-]+\s+dot\s+(?:com|net|org|io|co|me|app|dev|gg|ly|info|biz|edu|gov|uk|ug)\b',
    re.IGNORECASE,
)
_QUESTION_OR_REQUEST = re.compile(
    r'[?？]|'
    r'^\s*(?:who|what|when|where|why|which|how)\b|'
    r'^\s*(?:can|could|would|will|should|do|does|did|is|are|am|has|have|'
    r'had|may|might|must)\s+(?:you|u|i|we|they|he|she|it|anyone|someone|'
    r'somebody|this|that)\b|'
    r'\b(?:anyone|anybody|someone|somebody)\s+(?:know|got|have|has|can|'
    r'could|will|would|able to)\b|'
    r'\b(?:please|tell me|let me know|i want to know|i need to know|'
    r'i was wondering|anyone know|does anyone know|can anyone tell me|'
    r'ask me|asking for|send me|send us|give me|give us|lend me|lend us|'
    r'loan me|loan us|share with me|share with us|drop me|drop us|'
    r'provide me|provide us|gimme|tip me|rain me)\b',
    re.IGNORECASE,
)
_SCAM_LANGUAGE = re.compile(
    r'\b(?:scam(?:mer|ming|med)?|fraud(?:ulent)?|hack(?:er|ing|ed)?)\b',
    re.IGNORECASE,
)
_ADVERTISING_OR_TRADING = re.compile(
    r'\b(?:buy|buying|bought|sell|selling|sold|sale|trade|trading|traded|'
    r'advert(?:ise|ising|isement)?|promot(?:e|ing|ion)|'
    r'service(?:s)?)\b',
    re.IGNORECASE,
)
_REPEATED_CHARACTER = re.compile(r'(.)\1{5,}', re.IGNORECASE)
_REPEATED_WORD = re.compile(
    r'\b([\w\']+)(?:[\s.!?,;:\'"\-]+\1){3,}\b',
    re.IGNORECASE,
)
_LEET_TRANSLATION = str.maketrans({
    '0': 'o',
    '1': 'i',
    '3': 'e',
    '4': 'a',
    '5': 's',
    '7': 't',
    '!': 'i',
    '@': 'a',
    '$': 's',
})

# Curated English profanity and insult terms informed by LDNOOBW (CC BY 4.0):
# https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words
_ABUSIVE_WORDS = (
    'arse', 'arsehole', 'ass', 'asshole', 'asshat', 'bastard', 'bitch',
    'bollocks', 'bullshit', 'cock', 'crap', 'cunt', 'damn', 'dick',
    'dickhead', 'douche', 'douchebag', 'dumb', 'dumbass', 'fuck', 'fucker',
    'fuckface', 'fucking', 'fuckoff', 'fuckyou', 'idiot', 'jackass', 'jerk',
    'loser', 'motherfucker', 'moron', 'piss', 'prick', 'scumbag', 'shit',
    'shithead', 'shitty', 'silly', 'slut', 'stupid', 'twat', 'wanker',
    'whore',
)
_ABUSIVE_WORD_PATTERN = re.compile(
    r'(?<!\w)(?:'
    + '|'.join(
        r'[\W_]*'.join(re.escape(character) for character in word)
        for word in sorted(_ABUSIVE_WORDS, key=len, reverse=True)
    )
    + r')(?!\w)',
    re.IGNORECASE,
)


def _contains_phone_number_in_words(message):
    for sequence in _PHONE_WORDS.finditer(message):
        digit_count = 0
        repeat_next = 1
        for word in re.findall(r'[a-z]+', sequence.group()):
            if word == 'and':
                continue
            if word == 'double':
                repeat_next = 2
            elif word == 'triple':
                repeat_next = 3
            else:
                digit_count += repeat_next
                repeat_next = 1
        if digit_count >= 7:
            return True
    return False


def get_chat_rule_violation(message):
    """Return a user-facing reason when chat text breaks a content rule."""
    normalized = unicodedata.normalize('NFKC', str(message)).casefold()
    normalized = re.sub(r'\s*(?:\[dot\]|\(dot\)|\[.\]|\(\.\)|\bdot\b)\s*', '.', normalized)

    if _PHONE_DIGITS.search(normalized) or _contains_phone_number_in_words(normalized):
        return 'Phone numbers and contact numbers are not allowed, including numbers written in words.'

    if _LINK.search(normalized):
        return 'Links and website addresses are not allowed in chat.'

    if _ABUSIVE_WORD_PATTERN.search(normalized.translate(_LEET_TRANSLATION)):
        return 'Abusive language and profanity are not allowed in chat.'

    letters = [character for character in message if character.isalpha()]
    if len(letters) >= 8 and sum(character.isupper() for character in letters) / len(letters) >= 0.75:
        return 'Excessive capital letters are not allowed in chat.'

    if _REPEATED_CHARACTER.search(message) or _REPEATED_WORD.search(normalized):
        return 'Repeated characters or words are considered spam and are not allowed.'

    if _QUESTION_OR_REQUEST.search(normalized):
        return 'Questions and requests are not allowed in chat.'

    if re.search(r'\b(?:loan|borrow|beg|donat(?:e|ion)|tip|rain)\b', normalized) and re.search(
        r'\b(?:me|us|my|our|please|anyone|somebody|someone|send|give|drop)\b',
        normalized,
    ):
        return 'Asking for loans, rain, tips, or donations is not allowed in chat.'

    if _SCAM_LANGUAGE.search(normalized):
        return 'Scams, fraud, and hacking-related messages are not allowed in chat.'

    if _ADVERTISING_OR_TRADING.search(normalized):
        return 'Advertising, trading, buying, selling, and service offers are not allowed in chat.'

    return None
