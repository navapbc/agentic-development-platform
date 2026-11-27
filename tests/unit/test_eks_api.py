from scripts import eks_api


def test_the_kubeconfig_reaches_the_private_api_through_the_forwarded_port() -> None:
    config = eks_api.kubeconfig(
        cluster="agentic-platform-eks",
        host="abc.gr7.us-east-1.eks.amazonaws.com",
        ca_data="Q0E=",
        port=9443,
        region="us-east-1",
        profile="agentic-platform",
    )

    cluster = config["clusters"][0]["cluster"]
    assert cluster["server"] == "https://127.0.0.1:9443"
    assert cluster["tls-server-name"] == "abc.gr7.us-east-1.eks.amazonaws.com"
    exec_spec = config["users"][0]["user"]["exec"]
    assert exec_spec["args"] == [
        "eks",
        "get-token",
        "--cluster-name",
        "agentic-platform-eks",
        "--region",
        "us-east-1",
    ]
    assert exec_spec["env"] == [{"name": "AWS_PROFILE", "value": "agentic-platform"}]
    assert config["current-context"] == eks_api.CONTEXT
