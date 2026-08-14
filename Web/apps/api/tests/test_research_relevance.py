from app.services.tavily_search import is_allowed_research_source


def test_low_score_source_is_rejected() -> None:
    assert not is_allowed_research_source(
        "上海互联网公司裁员流程",
        "介绍经济补偿、劳动合同解除通知等具体流程。",
        "2026-01-01",
        0.44,
        "example.com",
    )


def test_high_score_relevant_source_is_allowed() -> None:
    assert is_allowed_research_source(
        "上海互联网公司裁员补偿流程",
        "用人单位解除劳动合同时，需要依法处理经济补偿与书面通知。",
        "2026-01-01",
        0.82,
        "gov.example.com",
    )


def test_scraped_login_boilerplate_is_rejected() -> None:
    assert not is_allowed_research_source(
        "讲堂报名",
        "账号密码登录 手机号注册登录 忘记密码 打开微信 分享到我的朋友圈 Image 20",
        "2026-01-01",
        0.91,
        "example.com",
    )


def test_blocked_blog_domain_is_rejected() -> None:
    assert not is_allowed_research_source(
        "美国整队 中国接招",
        "与小说剧情无关的个人网志内容。",
        "2026-01-01",
        0.95,
        "classic-blog.udn.com",
    )
