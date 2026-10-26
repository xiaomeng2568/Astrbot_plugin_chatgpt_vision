"""Read numeric usage fields only. Never serialize provider responses or prompts."""
from dataclasses import dataclass


def field(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**9 else None


@dataclass(frozen=True)
class Usage:
    input: int | None = None
    output: int | None = None
    cached: int | None = None
    written: int | None = None

    def __post_init__(self):
        for name in ('input', 'output', 'cached', 'written'):
            object.__setattr__(self, name, number(getattr(self, name)))
        if self.input is None or (self.cached is not None and self.cached > self.input):
            object.__setattr__(self, 'cached', None)
        if self.input is None or (self.written is not None and self.written > self.input):
            object.__setattr__(self, 'written', None)
        if self.cached is not None and self.written is not None and self.cached + self.written > self.input:
            object.__setattr__(self, 'cached', None)
            object.__setattr__(self, 'written', None)


def parse_usage(usage, normalized=False):
    if usage is None:
        return Usage()
    if normalized or field(usage, 'input_other') is not None:
        other, cached = number(field(usage, 'input_other')), number(field(usage, 'input_cached'))
        # AstrBot defaults input_cached to zero even when the provider omitted it.
        total = other + cached if other is not None and cached is not None else None
        return Usage(total, number(field(usage, 'output')), cached if cached else None)
    if field(usage, 'prompt_tokens') is not None:
        details = field(usage, 'prompt_tokens_details')
        cached = field(usage, 'prompt_cache_hit_tokens', field(details, 'cached_tokens'))
        return Usage(number(field(usage, 'prompt_tokens')), number(field(usage, 'completion_tokens')),
                     number(cached), number(field(details, 'cache_write_tokens')))
    if field(usage, 'input_tokens') is not None:
        details = field(usage, 'input_tokens_details')
        cached = field(details, 'cached_tokens')
        written = field(details, 'cache_write_tokens')
        total = number(field(usage, 'input_tokens'))
        # Anthropic reports uncached input separately from read/write cache tokens.
        anthropic = (any(k in usage for k in ('cache_read_input_tokens', 'cache_creation_input_tokens'))
                     if isinstance(usage, dict) else
                     hasattr(usage, 'cache_read_input_tokens') or hasattr(usage, 'cache_creation_input_tokens'))
        if anthropic:
            cached = number(field(usage, 'cache_read_input_tokens'))
            written = number(field(usage, 'cache_creation_input_tokens'))
            total = total + cached + written if all(v is not None for v in (total, cached, written)) else None
        return Usage(total, number(field(usage, 'output_tokens')), number(cached), number(written))
    if field(usage, 'prompt_token_count') is not None:
        output, thoughts = number(field(usage, 'candidates_token_count')), number(field(usage, 'thoughts_token_count'))
        return Usage(number(field(usage, 'prompt_token_count')),
                     output + (thoughts or 0) if output is not None else None,
                     number(field(usage, 'cached_content_token_count')))
    return Usage()


def response_usage(response):
    raw = field(response, 'raw_completion')
    usage = field(raw, 'usage', field(raw, 'usage_metadata'))
    if usage is not None:
        return parse_usage(usage)
    usage = field(response, 'usage')
    return parse_usage(usage)


def safe_name(value, default='未知'):
    if not isinstance(value, str) or not value or len(value) > 160:
        return default
    if any(ord(c) < 32 for c in value) or any(s in value.lower() for s in ('://', 'bearer ', 'sk-', 'ghp_', 'data:')):
        return default
    return value


def response_model(response, fallback):
    return safe_name(field(field(response, 'raw_completion'), 'model', field(response, 'model', fallback)))
