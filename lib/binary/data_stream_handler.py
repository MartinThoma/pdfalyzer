"""
Class for handling binary data streams. Currently focused on font binaries.
"""

# TODO: rename BinaryStreamHandler
import re
from collections import defaultdict
from numbers import Number
from os import environ
from typing import Iterator, Pattern

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from lib.binary.bytes_decoder import BytesDecoder
from lib.binary.bytes_match import CAPTURE_BYTES, BytesMatch

from lib.detection.character_encodings import BOMS, CHAR_ENCODING_1ST_COLOR_NUMBER
from lib.detection.regex_match_metrics import RegexMatchMetrics
from lib.helpers.bytes_helper import (DANGEROUS_INSTRUCTIONS, clean_byte_string,
     get_bytes_before_and_after_sequence, print_bytes)
from lib.helpers.rich_text_helper import (DANGER_HEADER, console, console_width,
    generate_subtable, pad_header, subheading_width)
from lib.helpers.string_helper import generate_hyphen_line, print_section_header
from lib.util.adobe_strings import CURRENTFILE_EEXEC
from lib.util.logging import log


# Command line options
MAX_SIZE_TO_BE_WORTH_FORCE_DECODING_ENV_VAR = 'PDFALYZER_MAX_SIZE_TO_BE_WORTH_FORCE_DECODING'
MAX_SIZE_TO_BE_WORTH_FORCE_DECODING_VALUES = 256
SHORT_QUOTE_LENGTH = 128

# Bytes
ESCAPED_DOUBLE_QUOTE_BYTES = b'\\"'
ESCAPED_SINGLE_QUOTE_BYTES = b"\\'"
FRONT_SLASH_BYTE = b"/"

# Regexes used to create iterators to find quoted bytes/strings/etc of interest
QUOTE_REGEXES = {
    'backtick': re.compile(b'`(.*?)`', re.DOTALL),
    'guillemet': re.compile(b'\xab(.*?)\xbb', re.DOTALL),
    'escaped double quote': re.compile(ESCAPED_DOUBLE_QUOTE_BYTES + CAPTURE_BYTES + ESCAPED_DOUBLE_QUOTE_BYTES, re.DOTALL),
    'escaped single quote': re.compile(ESCAPED_SINGLE_QUOTE_BYTES + CAPTURE_BYTES + ESCAPED_SINGLE_QUOTE_BYTES, re.DOTALL),
    'front slash': re.compile(FRONT_SLASH_BYTE + CAPTURE_BYTES + FRONT_SLASH_BYTE, re.DOTALL),
}

# Tables
STATS_TABLE_HEADERS = ['Metric', 'Value']
EASY_DECODES_HEADERS = ['Encoding', 'Successful Unforced Decodes']


class DataStreamHandler:
    def __init__(self, _bytes: bytes, owner):
        self.bytes = _bytes
        self.owner = owner
        self.limit_decodes_larger_than = int(environ.get(MAX_SIZE_TO_BE_WORTH_FORCE_DECODING_ENV_VAR, MAX_SIZE_TO_BE_WORTH_FORCE_DECODING_VALUES))
        self.suppression_notice_queue = []
        self.regex_extraction_stats = defaultdict(lambda: RegexMatchMetrics())

    def check_for_dangerous_instructions(self) -> None:
        """Scan for all the strings in DANGEROUS_INSTRUCTIONS list and decode bytes around them"""
        print_section_header("Scanning Font Binary For Anything 'Mad Sus'...", style=DANGER_HEADER)

        for instruction in DANGEROUS_INSTRUCTIONS:
            instruction_regex = re.compile(re.escape(instruction), re.DOTALL)
            label = f"({BOMS[instruction]}) " if instruction in BOMS else clean_byte_string(instruction)
            self._process_regex_matches(instruction_regex, label)

    def force_decode_all_quoted_bytes(self) -> None:
        """Find all strings matching QUOTE_REGEXES (AKA between quote chars) and decode them with various encodings"""
        for quote_type, quote_regex in QUOTE_REGEXES.items():
            print_section_header(f"Forcing Decode of {quote_type.capitalize()} Quoted Strings", style='color(100)')
            self._process_regex_matches(quote_regex, label=quote_type)

    # These extraction iterators will iterate over all the matches they find for the regex they pass
    # to extract_regex_capture_bytes() as an argument
    def extract_guillemet_quoted_bytes(self) -> Iterator[BytesMatch]:
        """Iterate on all strings surrounded by Guillemet quotes, e.g. «string»"""
        return self.extract_regex_capture_bytes(QUOTE_REGEXES['guillemet'])

    def extract_backtick_quoted_bytes(self):
        """Returns an interator over all strings surrounded by backticks"""
        return self.extract_regex_capture_bytes(QUOTE_REGEXES['backtick'])

    def extract_front_slash_quoted_bytes(self):
        """Returns an interator over all strings surrounded by front_slashes (hint: regular expressions)"""
        return self.extract_regex_capture_bytes(QUOTE_REGEXES['front_slaash'])

    def extract_regex_capture_bytes(self, regex: Pattern[bytes]) -> Iterator[BytesMatch]:
        """Finds all matches of regex_with_one_capture in self.bytes and calls yield() with BytesMatch tuples"""
        for i, match in enumerate(regex.finditer(self.bytes, self._eexec_idx())):
            yield(BytesMatch(match, ordinal=i))

    def print_stream_preview(self, num_bytes=None, title_suffix=None) -> None:
        """Print a preview showing the beginning and end of the stream data"""
        num_bytes = num_bytes or console_width()
        snipped_byte_count = self.stream_length() - (num_bytes * 2)

        if snipped_byte_count < 0:
            title = f"All {self.stream_length()} bytes in stream"
        else:
            title = f"First and last {num_bytes} bytes of {self.stream_length()} byte stream"

        console.line()
        title += title_suffix if title_suffix is not None else ''
        console.print(Panel(title, style='bytes_title', expand=False))
        console.print(generate_hyphen_line(title='BEGIN BYTES'), style='dim')

        if snipped_byte_count < 0:
            print_bytes(self.bytes)
        else:
            print_bytes(self.bytes[:num_bytes])
            console.print(f"\n    <...skip {snipped_byte_count} bytes...>\n", style='dim')
            print_bytes(self.bytes[-num_bytes:])

        console.print(generate_hyphen_line(title='END BYTES'), style='dim')
        console.line()

    def bytes_after_eexec_statement(self) -> bytes:
        """Get the bytes after the 'eexec' demarcation line (if it appears). See Adobe docs for details."""
        return self.bytes.split(CURRENTFILE_EEXEC)[1] if CURRENTFILE_EEXEC in self.bytes else self.bytes

    def stream_length(self) -> int:
        """Returns the number of bytes in the stream"""
        return len(self.bytes)

    def print_stats(self) -> None:
        console.line()
        self.owner.print_header_panel()
        console.line()
        stats_table = generate_stats_table()

        for regex, stats in self.regex_extraction_stats.items():
            if stats.match_count == 0:
                log.warn(f"There's not much to see here - 0 stats for {regex.pattern} so we will leave it out of table")
                continue

            regex_subtable = generate_subtable(cols=STATS_TABLE_HEADERS, header_style='subtable')
            decodes_subtable = generate_subtable(cols=EASY_DECODES_HEADERS, header_style='subtable')

            for metric, measure in vars(stats).items():
                if isinstance(measure, Number):
                    regex_subtable.add_row(metric, str(measure))

            for i, (encoding, easy_count) in enumerate(stats.were_matched_bytes_decodable.items()):
                style = f"color({CHAR_ENCODING_1ST_COLOR_NUMBER + 2 * i})"
                decodes_subtable.add_row(Text(encoding, style=style), str(easy_count))

            stats_table.add_row(str(regex.pattern), regex_subtable, decodes_subtable)

        console.print(stats_table)

    def _process_regex_matches(self, regex: Pattern[bytes], label: str) -> None:
        for bytes_match in self.extract_regex_capture_bytes(regex):
            self.regex_extraction_stats[regex].match_count += 1
            self.regex_extraction_stats[regex].bytes_matched += bytes_match.capture_len
            self.regex_extraction_stats[regex].bytes_match_objs.append(bytes_match)

            # Send suppressed decodes to a queue and track the reason for the suppression in the stats
            if bytes_match.capture_len > self.limit_decodes_larger_than or bytes_match.capture_len == 0:
                self._add_suppression_notice(bytes_match, label)
                continue

            # clear the suppressed notices queue before printing non suppressed matches
            self._print_suppression_notices()

            # Call up a BytesDecoder to do the actual decoding attempts
            decoder_label = label or clean_byte_string(regex.pattern)
            surrounding_bytes = get_bytes_before_and_after_sequence(self.bytes, bytes_match)
            decoder = BytesDecoder(surrounding_bytes, bytes_match, decoder_label)
            decoder.force_print_with_all_encodings()

            # Record stats
            self.regex_extraction_stats[regex].matches_decoded += 1
            console.line()

            for encoding, count in decoder.were_matched_bytes_decodable.items():
                if encoding not in self.regex_extraction_stats[regex].were_matched_bytes_decodable:
                    self.regex_extraction_stats[regex].were_matched_bytes_decodable[encoding] = count
                else:
                    self.regex_extraction_stats[regex].were_matched_bytes_decodable[encoding] += count

        if self.regex_extraction_stats[regex].match_count == 0:
            console.print(f"{regex.pattern} was not found for {label}...", style='dim')

    def _add_suppression_notice(self, bytes_match: BytesMatch, quote_type: str) -> None:
        """Print a message indicating that we are not going to decode a given block of bytes"""
        if bytes_match.capture_len == 0:
            msg = f"  Skipping zero length {quote_type} quoted bytes at {bytes_match.start_idx}...\n"
            console.print(msg, style='dark_grey_italic')
            self.regex_extraction_stats[regex].matches_skipped_for_being_empty += 1
            return

        msg = f"Suppressing decode of {bytes_match.capture_len} byte {quote_type} at "
        txt = Text(msg + f"position {bytes_match.start_idx} (", style='bytes_title')
        txt.append(f"--max-decode-length option is set to {self.limit_decodes_larger_than} bytes", style='grey')
        txt.append(')', style='bytes_title dim')
        log.debug(Text('ADDING to suppression notice queue: ') + txt)
        self.suppression_notice_queue.append(txt)
        self.regex_extraction_stats[bytes_match.regex].matches_skipped_for_being_too_big += 1

    def _print_suppression_notices(self):
        """Use a queue to Print in a group when possible and reset queue"""
        if len(self.suppression_notice_queue) == 0:
            return

        log.debug(f"printing {len(self.suppression_notice_queue)} suppression notices")
        suppression_notices_txt = Text("\n").join([notice for notice in self.suppression_notice_queue])
        panel = build_suppression_notice_panel(suppression_notices_txt)
        console.print(panel)
        self._reset_queue()

    def _reset_queue(self):
        self.current_suppression_notice_panel = None
        self.suppression_notice_queue = []

    def _eexec_idx(self) -> int:
        """Returns the location of CURRENTFILES_EEXEC within the binary stream dataor 0"""
        return self.bytes.find(CURRENTFILE_EEXEC) if CURRENTFILE_EEXEC in self.bytes else 0


def build_suppression_notice_panel(txt):
    """Just a panel"""
    return Panel(txt, style='bytes', expand=False)


def generate_stats_table():
    stats_table = Table(
        min_width=subheading_width(),
        show_lines=True,
        padding=[0,1],
        style='color(18) dim',
        border_style='color(87) ',
        header_style='color(8) reverse bold on black')

    stats_table.add_column(pad_header('Pattern'), justify='right', vertical='middle', style='color(25) bold reverse')
    stats_table.add_column(pad_header('Stats'), overflow='fold', justify='center')
    stats_table.add_column(pad_header('Level of Force'))
    return stats_table