from plugins.context_engine.object_context.fenced_code import fenced_code_blocks


def test_extracts_complete_backtick_fence_with_exact_body_and_offsets():
    source = "before\n```python title=demo.py\nprint('hi')\n```\nafter\n"

    blocks = fenced_code_blocks(source)

    assert len(blocks) == 1
    block = blocks[0]
    assert block.language == "python"
    assert block.info_string == "python title=demo.py"
    assert block.code == "print('hi')\n"
    assert source[block.start_offset : block.end_offset] == (
        "```python title=demo.py\nprint('hi')\n```\n"
    )
    assert source[block.code_start_offset : block.code_end_offset] == block.code


def test_longer_outer_fence_allows_shorter_fence_inside_code():
    source = "````markdown\n```python\nx = 1\n```\n````\n"

    blocks = fenced_code_blocks(source)

    assert len(blocks) == 1
    assert blocks[0].language == "markdown"
    assert blocks[0].code == "```python\nx = 1\n```\n"


def test_tilde_fence_and_crlf_are_preserved():
    source = "  ~~~js\r\nconst x = 1;\r\n  ~~~~\r\n"

    [block] = fenced_code_blocks(source)

    assert block.fence_char == "~"
    assert block.fence_length == 3
    assert block.code == "const x = 1;\r\n"
    assert source[block.start_offset : block.end_offset] == source


def test_unclosed_fence_is_not_extracted():
    assert fenced_code_blocks("before\n```python\nx = 1\n") == []


def test_inline_backticks_and_four_space_indented_fence_are_not_extracted():
    source = "Use `x = 1`.\n    ```python\n    x = 1\n    ```\n"
    assert fenced_code_blocks(source) == []


def test_backtick_in_backtick_fence_info_string_is_invalid():
    source = "```py`bad\nx = 1\n```\n"
    assert fenced_code_blocks(source) == []
