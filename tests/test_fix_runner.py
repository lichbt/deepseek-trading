import fix_runner


class FakeSession:
    def __init__(self):
        self.rate = {}
        self.quotes = {}
        self.positions = {}

    def open_pos_ids(self):
        return {'p1': 1000}


class FakeAdapter:
    symbol = 'EURUSD'

    def __init__(self):
        self.fix = FakeSession()

    def open_pos_ids(self):
        return {'p1': 1000}

    def cancel_stop(self, ref, side):
        pass

    def close_position(self, pos_id, units, side):
        return {'ord_status': '8', 'reject': 'MARKET_CLOSED'}


class FakePrice:
    def get_current_price(self):
        return 1.0


def test_rejected_close_keeps_state(monkeypatch, tmp_path):
    monkeypatch.setattr(fix_runner, 'STATE_FILE', str(tmp_path / 'state.json'))
    sleeve = {
        'sid': 's1', 'inst': 'EUR_USD', 'params': {'stop_mult': 2},
        'fn': lambda df, params: [0], 'ws': 1, 'decay_kelly_scale': 1,
    }
    state = {'s1': {'signal': 1, 'pos_id': 'p1', 'units': 1000, 'side': 1,
                    'stop': 0.5, 'stop_ref': {'clid': 'c'}}}
    fake = FakeAdapter()
    monkeypatch.setattr(fix_runner, 'latest', lambda s: (0, 1.0, 0.01, 1.0))
    monkeypatch.setattr(fix_runner, 'size_units', lambda *a, **k: (1000, (1000, 1000)))
    monkeypatch.setattr(fix_runner, 'min_lot_implied_risk', lambda *a, **k: (1000, (1000, 1000), 0.01))
    fix_runner.run_once([sleeve], state, live=True, adapters={
        'fix': {'EUR_USD': fake},
        'price': {'EUR_USD': FakePrice()},
        'equity': lambda: 2500,
    })
    assert state['s1']['pos_id'] == 'p1'


def test_software_stop_cancels_broker_stop_before_close(monkeypatch, tmp_path):
    monkeypatch.setattr(fix_runner, 'STATE_FILE', str(tmp_path / 'state.json'))
    sleeve = {
        'sid': 's1', 'inst': 'EUR_USD', 'params': {'stop_mult': 2},
        'fn': lambda df, params: [0], 'ws': 1, 'decay_kelly_scale': 1,
    }
    state = {'s1': {'signal': 1, 'pos_id': 'p1', 'units': 1000, 'side': 1,
                    'stop': 1.1, 'stop_ref': {'clid': 'c', 'order_id': 'o'}}}
    fake = FakeAdapter()
    calls = []
    fake.cancel_stop = lambda ref, side: calls.append(('cancel', ref, side)) or {'ord_status': '4'}
    fake.close_position = lambda pos_id, units, side: calls.append(('close', pos_id, units, side)) or {'ord_status': '2'}
    monkeypatch.setattr(fix_runner, 'latest', lambda s: (1, 1.0, 0.01, 1.0))
    monkeypatch.setattr(fix_runner, 'min_lot_implied_risk', lambda *a, **k: (1000, (1000, 1000), 0.01))
    fix_runner.run_once([sleeve], state, live=True, adapters={
        'fix': {'EUR_USD': fake},
        'price': {'EUR_USD': FakePrice()},
        'equity': lambda: 2500,
    })
    assert [call[0] for call in calls] == ['cancel', 'close']
    assert state['s1']['pos_id'] is None


def test_software_stop_keeps_state_when_cancel_unconfirmed(monkeypatch, tmp_path):
    monkeypatch.setattr(fix_runner, 'STATE_FILE', str(tmp_path / 'state.json'))
    sleeve = {
        'sid': 's1', 'inst': 'EUR_USD', 'params': {'stop_mult': 2},
        'fn': lambda df, params: [0], 'ws': 1, 'decay_kelly_scale': 1,
    }
    state = {'s1': {'signal': 1, 'pos_id': 'p1', 'units': 1000, 'side': 1,
                    'stop': 1.1, 'stop_ref': {'clid': 'c', 'order_id': 'o'}}}
    fake = FakeAdapter()
    fake.cancel_stop = lambda ref, side: None
    fake.close_position = lambda *args: (_ for _ in ()).throw(AssertionError('close raced cancel'))
    monkeypatch.setattr(fix_runner, 'latest', lambda s: (1, 1.0, 0.01, 1.0))
    monkeypatch.setattr(fix_runner, 'min_lot_implied_risk', lambda *a, **k: (1000, (1000, 1000), 0.01))
    fix_runner.run_once([sleeve], state, live=True, adapters={
        'fix': {'EUR_USD': fake},
        'price': {'EUR_USD': FakePrice()},
        'equity': lambda: 2500,
    })
    assert state['s1']['pos_id'] == 'p1'
