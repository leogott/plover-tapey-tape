import collections
import itertools
import json
import re
from datetime import datetime
from pathlib  import Path

import plover

class TapeyTape:
    SHOW_WHITESPACE = str.maketrans({'\n': '\\n', '\r': '\\r', '\t': '\\t'})

    @staticmethod
    def retroformat(translations):
        return ''.join(plover.formatting.RetroFormatter(translations).last_fragments(0))

    @staticmethod
    def expand(format_string, items):
        return re.sub('%(.)', lambda match: items.get(match.group(1), ''), format_string)

    @staticmethod
    def is_fingerspelling(translation):
        # For simplicity, just equate glue with fingerspelling for now
        return any(action.glue for action in translation.formatting)

    @staticmethod
    def is_whitespace(translation):
        return all(not action.text or action.text.isspace() for action in translation.formatting)

    def __init__(self, engine):
        self.engine = engine

        self.last_stroke_time   = None
        self.was_fingerspelling = False

    def get_suggestions(self, translations):
        text = self.retroformat(translations)
        stroke_count = sum(len(translation.rtfcre) for translation in translations)
        return [outline
                for suggestion in self.engine.get_suggestions(text)
                for outline in suggestion.steno_list
                if len(outline) < stroke_count]

    def start(self):
        # Config
        config_dir = Path(plover.oslayer.config.CONFIG_DIR)
        try:
            with config_dir.joinpath('tapey_tape.json').open() as f:
                config = json.load(f)
        except FileNotFoundError:
            config = {}

        try:
            # Set lower bound to some small non-zero number to avoid division by zero
            self.bar_time_unit = max(float(config['bar_time_unit']), 0.01)
        except (KeyError, ValueError):
            self.bar_time_unit = 0.2
        try:
            self.bar_max_width = min(max(int(config['bar_max_width']), 0), 100)
        except (KeyError, ValueError):
            self.bar_max_width = 5

        self.output_style = config.get('output_style')
        if self.output_style != 'translation':
            self.output_style = 'definition'

        output_format = config.get('output_format')
        if not isinstance(output_format, str):
            output_format = '%b |%s| %t  %h'
        self.left_format, *rest = re.split(r'(\s*%h)', output_format, maxsplit=1)
        self.right_format = ''.join(rest)

        # e.g., 1- -> S-, 2- -> T-, etc.
        self.numbers = {number: letter for letter, number in plover.system.NUMBERS.items()}

        self.engine.hook_connect('stroked', self.on_stroked)

        self.file = config_dir.joinpath('tapey_tape.txt').open('a')

    def stop(self):
        if self.was_fingerspelling:
            self.file.write(self.expand(self.right_format, self.items).rstrip())
            self.file.write('\n')

        self.engine.hook_disconnect('stroked', self.on_stroked)

        self.file.close()

    def on_stroked(self, stroke):
        # Translation stack
        translations = self.engine.translator_state.translations

        # Add back what was delayed
        if self.was_fingerspelling:
            # Some important cases to consider in deciding whether to show suggestions:
            #
            # word &f &o &o word
            #   Stack: word
            #          word &f
            #          word &f &o
            #          word &f &o &o       (last)
            #          word &f &o &o word  (current)
            #   This is the most typical case. The last last-translation was fingerspelling,
            #   and the current last-translation is not, representing a turning point.
            #   Show suggestions for "foo" after the last &o.
            #
            # word &f &o &o word &b *
            #   Stack: word
            #          word &f
            #          word &f &o
            #          word &f &o &o
            #          word &f &o &o word
            #          word &f &o &o word &b
            #          word &f &o &o word
            #   Here it's also the case that the last last-translation was fingerspelling,
            #   and the current last-translation is not, representing a "turning point" --
            #   but not in the direction we want. If we don't handle these undo strokes
            #   specially, suggestions for "foo" would be shown after &b.
            #
            # Now, assume the user defines PW*/A*/R* as "BAR" in their dictionary.
            # Not sure why anyone would want to do something like that, but it's possible.
            #
            # word &f &o &o &b &a &r
            #   Stack: word
            #          word &f
            #          word &f &o
            #          word &f &o &o
            #          word &f &o &o &b
            #          word &f &o &o &b &a
            #          word &f &o &o BAR
            #   Again, this represents a "turning point" where we shouldn't show suggestions.
            #   If we don't handle this case specially, suggestions for "foo" would be shown
            #   after &a. We can identify this case by looking at whether anything got replaced
            #   in the current last-translation.
            if (not translations
                    or self.is_fingerspelling(translations[-1])
                    or stroke.is_correction
                    or translations[-1].replaced):
                self.items['h'] = '' # suppress suggestions

            self.file.write(self.expand(self.right_format, self.items).rstrip())
            self.file.write('\n')

        # Bar
        now     = datetime.now()
        seconds = 0 if self.last_stroke_time is None else (now - self.last_stroke_time).total_seconds()
        width   = min(int(seconds / self.bar_time_unit), self.bar_max_width)
        bar     = ('+' * width).rjust(self.bar_max_width)

        self.last_stroke_time = now

        # Steno
        keys = set()
        for key in stroke.steno_keys:
            if key in self.numbers:                # e.g., if key is 1-
                keys.add(self.numbers[key])        #   add the corresponding S-
                keys.add(plover.system.NUMBER_KEY) #   and #
            else:                                  # if key is S-
                keys.add(key)                      #   add S-
        steno = ''.join(key.strip('-') if key in keys else ' ' for key in plover.system.KEYS)

        # At this point we start to deal with things for which we need to
        # examine the translation stack: output, suggestions, and determining
        # whether the current stroke is a fingerspelling stroke.

        if stroke.is_correction or not translations:
            # If the stroke is an undo stroke, just output * and call it a day.
            # (Sometimes it can be technically correct to show translations on
            # an undo stroke. For example:
            #   SPWOBGS +sandbox
            #   KAEUGS  -sandbox +intoxication
            #   *       -intoxication +sandbox
            # "sandbox" can be thought of as "translation" of the undo stroke.
            # But
            #   | S  PW   O      B G S  | sandbox
            #   |   K    A  EU     G S  | *intoxication
            #   |          *            | *sandbox
            # is probably not what the user expects.)
            output      = '*'
            suggestions = ''
            self.was_fingerspelling = False
        else:
            # We can now rest assured that the translation stack is non-empty.

            # Output
            star = '*' if len(translations[-1].strokes) > 1 else ''
            # Here the * means something different: it doesn't mean that the
            # stroke is an undo stroke but that the translation is corrected.
            # (Note that Plover doesn't necessarily need to pop translations
            # from the stack to correct a translation. For example, there is
            # this (unnecessary) definition in main.json:
            #   "TP-PL/SO": "{.}so",
            # If you write TP-PL followed by SO, Plover just needs to push
            # "so" to the stack and doesn't need to pop {.}. Or maybe it does
            # pop {.}; it doesn't matter to us, because we can't see it from
            # the snapshots we get on stroked events anyway.)

            if self.output_style == 'translation':
                formatted = self.retroformat(translations[-1:])
            else:
                definition = translations[-1].english
                formatted = '/' if definition is None else definition
                # TODO: don't show numbers as untranslate

            output = star + formatted.translate(self.SHOW_WHITESPACE)

            # Suggestions
            suggestions = []

            if not self.is_whitespace(translations[-1]):
                buffer = []
                deque  = collections.deque()
                for translation in reversed(translations):
                    if self.is_fingerspelling(translation):
                        buffer.append(translation)
                    else:
                        if buffer:
                            deque.extendleft(buffer)
                            buffer = []
                            suggestions.append(self.get_suggestions(deque))
                        deque.appendleft(translation)
                        if not self.is_whitespace(translation):
                            suggestions.append(self.get_suggestions(deque))
                if buffer:
                    deque.extendleft(buffer)
                    suggestions.append(self.get_suggestions(deque))

            suggestions = ' '.join('>' * i + ' '.join(map('/'.join, outlines))
                                   for i, outlines in enumerate(suggestions, start=1)
                                   if outlines)

            self.was_fingerspelling = self.is_fingerspelling(translations[-1])

        self.items = {'b': bar,
                      's': steno,
                      't': output,      # "t" for "translation"
                      'h': suggestions, # "h" for "hint"
                      '%': '%'}

        self.file.write(self.expand(self.left_format, self.items))

        if not self.was_fingerspelling:
            self.file.write(self.expand(self.right_format, self.items).rstrip())
            self.file.write('\n')

        self.file.flush()
