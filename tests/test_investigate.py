from snitch.investigate import correlate, error_signature, rollback_target

DEPLOYS = [
    {"version": "v2.13.1", "at": 1000, "sha": "a"},
    {"version": "v2.13.2", "at": 2000, "sha": "b"},
    {"version": "v2.14.0", "at": 3000, "sha": "c"},
]


def err(at, version="v2.14.0", msg="POST /checkout 500 KeyError: 'JPY' currency=JPY"):
    return {"at": at, "level": "ERROR", "version": version, "message": msg}


def test_names_the_deploy_right_before_the_errors():
    v = correlate(DEPLOYS, [err(3010), err(3020), err(3030)])
    assert v.suspect["version"] == "v2.14.0"
    assert v.previous["version"] == "v2.13.2"
    assert v.confidence > 0.9
    assert v.error_count == 3


def test_low_confidence_when_errors_long_after_deploy():
    v = correlate(DEPLOYS, [err(3000 + 7200, version="")])
    assert v.suspect["version"] == "v2.14.0"
    assert v.confidence < 0.5


def test_no_errors_defaults_to_latest():
    v = correlate(DEPLOYS, [])
    assert v.suspect["version"] == "v2.14.0"
    assert v.first_error_at is None


def test_signature_strips_variable_parts():
    sig = error_signature([err(1, msg="boom currency=JPY"), err(2, msg="boom currency=XYZ"), err(3, msg="other")])
    assert sig == "boom"


def test_rollback_target_skips_repeat_bad_deploys():
    deploys = DEPLOYS + [{"version": "v2.14.0", "at": 4000}]
    assert rollback_target(deploys, "v2.14.0")["version"] == "v2.13.2"
    assert rollback_target(DEPLOYS[:1], "v2.13.1") is None
    assert rollback_target(DEPLOYS, "v9") is None
