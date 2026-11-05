def test_ci_probe_intentional_failure():
    """临时分支专用：故意失败以验证 CI 失败日志 artifact。验证后随分支删除。"""
    assert False, "CI artifact probe: intentional failure"
