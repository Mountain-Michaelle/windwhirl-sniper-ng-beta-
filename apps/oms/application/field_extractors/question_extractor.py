from __future__ import annotations

from typing import Optional


class QuestionExtractor:
    '''
    Extracts customer question(s) from the order.

    Captures everything after the question label.
    May span multiple lines. Never truncated.

    Label variants (normalized):
        "do you have any questions"
        "any questions"
        "questions"
        "customer question"
    '''

    QUESTION_LABELS = [
        "do you have any questions",
        "any questions",
        "questions",
        "question",
        "customer question",
        "remarks",
        "note",
        "notes",
        "additional note",
        "additional notes",
        "comment",
        "comments",
    ]

    def extract(self, text: str, sections: dict) -> Optional[str]:
        '''
        Extract customer question(s). Preserves multi-line content.
        Returns None if the customer wrote "No", "None", or similar.
        '''
        raw = self._find_section(sections)
        if not raw:
            return None

        value = raw.strip()

        # If customer explicitly said "No" or "None", no question
        if value.lower() in ("no", "none", "nope", "n/a", "nil", "nothing", "-"):
            return None

        if value:
            return value

        return None

    def _find_section(self, sections: dict) -> Optional[str]:
        for label in self.QUESTION_LABELS:
            if label in sections:
                return sections[label]
        return None
