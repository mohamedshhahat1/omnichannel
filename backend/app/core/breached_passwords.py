"""Breached-password screening.

`docs/security.md` 2.5 requires new passwords to be "checked against a
breached-password list". Phase 3 enforced length and surrounding whitespace
only, which lets `password1234` - a credential that appears in essentially
every public breach corpus - satisfy a twelve-character minimum.

The implementation is a **local** corpus, on purpose:

* No plaintext, no digest and no prefix of either leaves the process. A
  k-anonymity lookup against a third party (Have I Been Pwned's range API is
  the usual choice) would still disclose a five-character SHA-1 prefix of every
  password our users choose to a service we do not run, and the repository has
  no outbound HTTP dependency, no egress allowance and no vendor agreement that
  covers it. `security.md` 7 keeps credential material inside the process; a
  remote call is the one thing that cannot honour that.
* It cannot fail. An external screen has to answer the question "what do we do
  when the provider is down?", and both answers are bad: fail open and the
  control is theatre, fail closed and registration stops when a third party
  has an incident.

The trade is honest and is recorded in `security.md`: a local corpus catches
the passwords that automated credential-stuffing actually tries first, not the
full several-hundred-million-entry breach set. `BreachedPasswordScreen` takes
the corpus as a constructor argument precisely so a deployment can supply a
larger locally maintained list, or a later phase can subclass it with a
privacy-reviewed remote provider, without touching a call site.

Nothing in this module logs, stores, hashes or returns the candidate password.
The only thing that ever leaves it is a boolean.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final


def _normalize(value: str) -> str:
    """Fold a candidate for comparison, without altering what gets hashed.

    Case folding and stripping only. Whitespace inside the value is left alone
    deliberately: a multi-word passphrase is a good password, and collapsing
    its spaces would fold it onto a concatenated corpus entry and reject it.
    """
    return value.strip().casefold()


COMMON_BREACHED_PASSWORDS: Final[frozenset[str]] = frozenset(
    {
        # Top of every published breach analysis, in the forms people type them.
        "123456",
        "1234567",
        "12345678",
        "123456789",
        "1234567890",
        "12345678910",
        "123456789012",
        "1234567891011",
        "111111",
        "1111111111",
        "123123",
        "123123123",
        "112233",
        "121212",
        "000000",
        "654321",
        "987654321",
        "password",
        "password1",
        "password12",
        "password123",
        "password1234",
        "password12345",
        "password123456",
        "passw0rd",
        "passw0rd1",
        "passw0rd123",
        "passw0rd1234",
        "p@ssword",
        "p@ssw0rd",
        "p@ssw0rd123",
        "mypassword",
        "mypassword123",
        "newpassword",
        "newpassword1",
        "changeme",
        "changeme123",
        "letmein",
        "letmein123",
        "letmein12345",
        "welcome",
        "welcome1",
        "welcome123",
        "welcome12345",
        "welcome123456",
        "admin",
        "admin123",
        "admin1234",
        "administrator",
        "administrator123",
        "root",
        "rootroot",
        "toor",
        "guest",
        "guest123",
        "test",
        "test123",
        "testing123",
        "secret",
        "secret123",
        "default",
        "default123",
        # Keyboard walks, including the long ones a 12-character floor invites.
        "qwerty",
        "qwerty123",
        "qwerty1234",
        "qwerty123456",
        "qwertyuiop",
        "qwertyuiop123",
        "qwertyuiopasdfgh",
        "azerty",
        "azerty123456",
        "asdfghjkl",
        "asdfghjkl123",
        "zxcvbnm",
        "zxcvbnm123",
        "1q2w3e4r",
        "1q2w3e4r5t",
        "1q2w3e4r5t6y",
        "1qaz2wsx",
        "1qaz2wsx3edc",
        "zaq12wsx",
        "zaq1zaq1",
        "qazwsxedc",
        "qwe123456",
        "abc123",
        "abc123456",
        "abcd1234",
        "abcdefghijkl",
        "a1b2c3d4e5f6",
        # Words and names that dominate cracked-password corpora.
        "iloveyou",
        "iloveyou1",
        "iloveyou123",
        "iloveyou1234",
        "sunshine",
        "sunshine123",
        "princess",
        "princess123",
        "football",
        "football123",
        "baseball",
        "baseball123",
        "basketball",
        "superman",
        "superman123",
        "batman123",
        "starwars",
        "starwars123",
        "pokemon123",
        "monkey",
        "monkey123",
        "dragon",
        "dragon123",
        "shadow",
        "shadow123",
        "master",
        "master123",
        "hunter",
        "hunter123",
        "ranger",
        "harley",
        "buster",
        "soccer",
        "trustno1",
        "trustno1234",
        "whatever",
        "whatever123",
        "freedom",
        "freedom123",
        "computer",
        "computer123",
        "internet",
        "internet123",
        "michael",
        "michael123",
        "jennifer",
        "jennifer123",
        "jessica",
        "jessica123",
        "charlie",
        "charlie123",
        "thomas",
        "robert",
        "jordan",
        "daniel",
        "hannah",
        "summer",
        "chelsea",
        "michelle",
        "nicole",
        "amanda",
        "ashley",
        "bailey",
        "cookie",
        "purple",
        "orange",
        "access",
        # Product and vendor defaults that end up on internet-facing systems.
        "samsung",
        "google123",
        "facebook",
        "facebook123",
        "linkedin",
        "myspace1",
        "adobe123",
        "photoshop",
        "oracle",
        "postgres",
        "postgres123",
        "mysql123",
        "redis123",
        "docker123",
        "jenkins123",
        "kubernetes",
        "changeit",
        "secretpassword",
        "supersecret",
        "supersecret123",
        "letmein1234567",
        "iamawesome123",
        "nevergonnaguess",
    }
)
"""A conservative floor, not a complete breach corpus. See the module docstring."""


class BreachedPasswordScreen:
    """Rejects passwords that appear in a known-breached corpus.

    The corpus is an argument so a deployment can widen it - or a later phase
    can replace this class with a privacy-reviewed remote provider - without
    changing any caller.
    """

    def __init__(self, corpus: Iterable[str] = COMMON_BREACHED_PASSWORDS) -> None:
        self._corpus = frozenset(_normalize(entry) for entry in corpus)

    def is_breached(self, password: str) -> bool:
        """True when this password is known to be compromised.

        The candidate is never stored, logged or returned; only this boolean
        leaves the call.
        """
        candidate = _normalize(password)
        if not candidate:
            return False
        return candidate in self._corpus


DEFAULT_SCREEN: Final = BreachedPasswordScreen()


def is_breached(password: str) -> bool:
    """Screen a password against the default corpus."""
    return DEFAULT_SCREEN.is_breached(password)
