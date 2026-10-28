from nexfiscal.ui.start_policy import force_reprocess_for_start


def test_resume_and_retry_ignore_force_checkbox():
    assert force_reprocess_for_start(force_checked=True, from_db=True) is False


def test_explicit_fresh_run_honors_force_checkbox():
    assert force_reprocess_for_start(force_checked=True, from_db=False) is True
    assert force_reprocess_for_start(force_checked=False, from_db=False) is False
