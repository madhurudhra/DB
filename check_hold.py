from app.brokers.robinhood.crypto import RobinhoodCrypto
for h in RobinhoodCrypto().holdings().get('results', []):
    print(h)
