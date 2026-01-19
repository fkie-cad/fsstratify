import pytest

from fsstratify.utils import (
    parse_size_definition,
    merge_blocks_to_fragments,
    merge_overlapping_fragments,
    parse_boolean_string,
    get_random_string,
    parse_duration_string,
)


class TestGetRandomString:
    @pytest.mark.parametrize("length", (1, 2, 3, 10, 20, 100, 123))
    def test_that_the_generated_string_has_the_correct_length(self, length):
        assert len(get_random_string(length)) == length


class TestParseSizeDefinition:
    def test_that_an_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_size_definition("")

    @pytest.mark.parametrize("invalid", ("1 X", "2X", "3YiB", "4 YiB", "5Z", "5ZB"))
    def test_that_an_invalid_suffix_raises(self, invalid):
        with pytest.raises(ValueError):
            parse_size_definition(invalid)

    @pytest.mark.parametrize("integer", [1, 2, 3, 100, 1024, 547567832423])
    def test_that_simple_integers_work(self, integer):
        assert parse_size_definition(str(integer)) == integer

    @pytest.mark.parametrize(
        "size_str,expected",
        [
            ("123", 123),
            ("5k", 5000),
            ("74 k", 74000),
            ("13 KiB", 13312),
            ("14Ki", 14336),
        ],
    )
    def test_that_size_strings_are_parsed_correctly(self, size_str, expected):
        assert parse_size_definition(size_str) == expected


class TestParseDurationString:
    def test_that_an_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_duration_string("")

    @pytest.mark.parametrize(
        "invalid",
        ("1 X", "2X", "3YiB", "4 YiB", "5Z", "5ZB", "8m", "8 m", "1 2", "min 2"),
    )
    def test_that_an_invalid_suffix_raises(self, invalid):
        with pytest.raises(ValueError):
            parse_duration_string(invalid)

    @pytest.mark.parametrize("integer", [1, 2, 3, 100, 1024, 547567832423])
    def test_that_simple_integers_work(self, integer):
        assert parse_duration_string(str(integer)) == integer

    @pytest.mark.parametrize(
        "duration_str,expected",
        [
            ("123", 123),
            ("5s", 5),
            ("5min", 300),
            ("74 min", 4440),
            ("13h", 46800),
            ("13 h", 46800),
        ],
    )
    def test_that_size_strings_are_parsed_correctly(self, duration_str, expected):
        assert parse_duration_string(duration_str) == expected


class TestParseBooleanString:
    @pytest.mark.parametrize("invalid_input", ("", "xyz", "yes no", "yes abc", 1))
    def test_that_invalid_inputs_raise(self, invalid_input):
        with pytest.raises(ValueError):
            parse_boolean_string(invalid_input)

    @pytest.mark.parametrize(
        "bool_value", ("True", "true", "TRUE", "TrUe", "yes", "YES", "yEs", "y", "Y")
    )
    def test_that_true_values_are_parsed_correctly(self, bool_value):
        assert parse_boolean_string(bool_value) is True

    @pytest.mark.parametrize(
        "bool_value", ("False", "false", "FALSE", "FaLsE", "no", "NO", "No", "n", "N")
    )
    def test_that_false_values_are_parsed_correctly(self, bool_value):
        assert parse_boolean_string(bool_value) is False


class TestMergeBlocksToFragments:
    def test_that_an_empty_list_returns_an_empty_list(self):
        assert merge_blocks_to_fragments([]) == []

    @pytest.mark.parametrize(
        "block_list,expected",
        [
            ((1, 2, 3), [(1, 3)]),
            ((1, 2, 3, 5, 6), [(1, 3), (5, 6)]),
            ((1, 3, 5), [(1, 1), (3, 3), (5, 5)]),
            (
                (1, 3, 4, 7, 9, 10, 11, 12, 17),
                [(1, 1), (3, 4), (7, 7), (9, 12), (17, 17)],
            ),
        ],
    )
    def test_that_blocks_are_merged_correctly(self, block_list, expected):
        assert merge_blocks_to_fragments(block_list) == expected


class TestMergeOverlappingFragments:
    def test_that_an_empty_list_returns_an_empty_list(self):
        assert merge_overlapping_fragments([]) == []

    @pytest.mark.parametrize(
        "fragments", ([(1, 2)], [(1, 2), (4, 6)], [(1, 2), (4, 6), (8, 13)])
    )
    def test_that_non_overlapping_lists_are_left_untouched(self, fragments):
        assert fragments == merge_overlapping_fragments(fragments)

    @pytest.mark.parametrize(
        "fragments,expected",
        (
            ([(1, 2), (2, 3)], [(1, 3)]),
            ([(1, 2), (2, 3), (3, 4)], [(1, 4)]),
            ([(1, 2), (2, 3), (2, 4)], [(1, 4)]),
            ([(1, 2), (2, 3), (5, 5)], [(1, 3), (5, 5)]),
            ([(1, 2), (2, 3), (5, 6), (1, 8)], [(1, 8)]),
        ),
    )
    def test_that_overlapping_fragments_are_merged(self, fragments, expected):
        assert merge_overlapping_fragments(fragments) == expected

    @pytest.mark.parametrize(
        "fragments,expected",
        (
            ([(1, 2), (3, 4)], [(1, 4)]),
            ([(1, 2), (3, 4), (4, 5), (6, 8)], [(1, 8)]),
            ([(1, 2), (3, 4), (5, 8), (9, 10)], [(1, 10)]),
            ([(3, 5), (1, 2)], [(1, 5)]),
            ([(3, 5), (1, 2), (6, 8)], [(1, 8)]),
            ([(1, 5), (6, 10)], [(1, 10)]),
        ),
    )
    def test_that_consecutive_fragments_are_merged(self, fragments, expected):
        assert merge_overlapping_fragments(fragments) == expected


class TestParsePatternFormatString:
    def test_that_valid_pattern_is_parsed_correctly(self):
        from fsstratify.utils import parse_pattern_format_string

        result = parse_pattern_format_string("pattern(10, %s, hello)")
        assert result == (10, "%s", "hello")

    def test_that_pattern_with_complex_format_string_works(self):
        from fsstratify.utils import parse_pattern_format_string

        result = parse_pattern_format_string("pattern(5, %c%f, test)")
        assert result == (5, "%c%f", "test")

    def test_that_invalid_pattern_returns_none(self):
        from fsstratify.utils import parse_pattern_format_string

        assert parse_pattern_format_string("invalid") is None
        assert parse_pattern_format_string("pattern()") is None
        assert parse_pattern_format_string("pattern(abc, %s, test)") is None


class TestSplitOnFirstAndLast:
    def test_that_string_is_split_correctly(self):
        from fsstratify.utils import split_on_first_and_last

        result = split_on_first_and_last("a,b,c", ",")
        assert result == ("a", "b", "c")

    def test_that_multiple_separators_work(self):
        from fsstratify.utils import split_on_first_and_last

        result = split_on_first_and_last("first,middle,part,last", ",")
        assert result == ("first", "middle,part", "last")

    def test_that_insufficient_separators_raise_error(self):
        from fsstratify.utils import split_on_first_and_last

        with pytest.raises(ValueError):
            split_on_first_and_last("no_separator", ",")

        with pytest.raises(ValueError):
            split_on_first_and_last("one,separator", ",")


class TestExtractFromParentheses:
    def test_that_content_is_extracted_correctly(self):
        from fsstratify.utils import extract_from_parentheses

        assert extract_from_parentheses("func(content)") == "content"
        assert extract_from_parentheses("(simple)") == "simple"
        assert extract_from_parentheses("prefix(inner)suffix") == "inner"

    def test_that_nested_parentheses_extract_outermost(self):
        from fsstratify.utils import extract_from_parentheses

        assert extract_from_parentheses("(outer(inner))") == "outer(inner)"

    def test_that_missing_parentheses_raise_error(self):
        from fsstratify.utils import extract_from_parentheses

        with pytest.raises(ValueError):
            extract_from_parentheses("no_parens")

        with pytest.raises(ValueError):
            extract_from_parentheses("only(open")

        with pytest.raises(ValueError):
            extract_from_parentheses("only)close")


class TestParseFormatString:
    def test_that_valid_format_specifiers_are_parsed(self):
        from fsstratify.utils import parse_format_string

        result = parse_format_string("test%cfile")
        assert len(result) == 1
        assert result[0][2] == "%c"

    def test_that_multiple_specifiers_are_parsed(self):
        from fsstratify.utils import parse_format_string

        result = parse_format_string("%f%s%c")
        assert len(result) == 3

    def test_that_S_specifier_allowed_only_once(self):
        from fsstratify.utils import parse_format_string

        # Single %S should work
        result = parse_format_string("test%Sfile")
        assert len(result) == 1

        # Multiple %S should raise
        with pytest.raises(ValueError, match="%S.*only once"):
            parse_format_string("%S%S")

    def test_that_escaped_percent_is_ignored(self):
        from fsstratify.utils import parse_format_string

        result = parse_format_string("100%% complete %c")
        assert len(result) == 1
        assert result[0][2] == "%c"
