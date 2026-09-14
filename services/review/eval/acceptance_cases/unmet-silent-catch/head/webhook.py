def deliver(webhook, payload):
    try:
        webhook.post(payload)
    except DeliveryError:
        pass
