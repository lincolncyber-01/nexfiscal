"""Políticas puras de início/retomada, independentes da interface gráfica."""


def force_reprocess_for_start(force_checked: bool, from_db: bool) -> bool:
    """Retomada/tentativa não deve reprocessar concluídas por causa da caixa de seleção."""
    return bool(force_checked and not from_db)
