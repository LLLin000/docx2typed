from scripts.real_acceptance import run_acceptance


def test_real_acceptance_workflow(tmp_path):
    report = run_acceptance(tmp_path / "acceptance")

    assert report["result"] == "PASS"
    assert all(check["status"] == "PASS" for check in report["checks"])
