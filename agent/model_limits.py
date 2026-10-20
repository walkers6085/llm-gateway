"""Метаданные моделей, реальные окна и независимые лимиты бюджета."""
import json
import math

# Паспортные значения Z.ai; метаданные конкретного сервера имеют приоритет.
# Источники: docs.z.ai/guides/llm/glm-4.5, glm-4.7, glm-5.1.
KNOWN_WINDOWS = {
    'glm-4.5': 131072, 'glm-4.5-air': 131072, 'glm-4.5-flash': 131072,
    'glm-4.7': 200000, 'glm-4.7-flash': 200000, 'glm-5.1': 200000,
}
FALLBACK_WINDOW = 131072


def positive(value):
    return value if type(value) is int and 0 < value <= 100_000_000 else None


def metadata(record):
    """Читаем только числовые лимиты, не сохраняем произвольные данные каталога."""
    result = {}
    for source in (record.get('top_provider'), record.get('limits'), record.get('limit'), record):
        if not isinstance(source, dict):
            continue
        for target, keys in (
            ('context_window', ('context_window', 'context_length', 'max_context_length', 'max_model_len', 'context')),
            ('max_output_tokens', ('max_output_tokens', 'max_completion_tokens', 'output')),
        ):
            if target not in result:
                value = next((positive(source.get(key)) for key in keys if positive(source.get(key))), None)
                if value:
                    result[target] = value
    return result


def resolve(settings, model, catalog=None):
    profile = settings.get('model_profiles', {}).get(model, {})
    api_model = profile.get('options', {}).get('api_model', model)
    reported = (catalog or {}).get(api_model, (catalog or {}).get(model, {}))
    window = positive(profile.get('context_window'))
    source = 'config'
    if not window:
        window, source = positive(reported.get('context_window')), 'api'
    if not window:
        window, source = KNOWN_WINDOWS.get(api_model.lower()), 'catalog'
    if not window:
        window, source = FALLBACK_WINDOW, 'fallback'
    output = profile.get('max_output_tokens', settings.get('max_output_tokens', 1024))
    output = min(output, positive(reported.get('max_output_tokens')) or output, max(1, window - 1))
    configured = profile.get('max_input_tokens', settings.get('max_input_tokens', 0))
    if source == 'fallback':
        window = max(window, (configured or 0) + output)
    # Явный лимит входа — пользовательское ограничение, а не паспортное окно модели.
    input_limit = min(configured or window - output, max(1, window - output))
    turn = profile.get('max_turn_tokens', settings.get('max_turn_tokens', 0))
    return {**settings, **profile, 'context_window': window, 'context_source': source,
            'context_known': source != 'fallback', 'max_input_tokens': input_limit,
            'input_limit_source': 'config' if configured else source,
            'max_output_tokens': output, 'max_turn_tokens': turn or max(65536, window * 4)}


def estimate_tokens(messages, tools=None):
    """Приблизительная оценка без токенизатора; байты не выдаются за токены."""
    return math.ceil(len(json.dumps([messages, tools or []], ensure_ascii=False).encode()) / 3) + 64
