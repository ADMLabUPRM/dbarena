import csv
import os
import random
import sys
from contextlib import ExitStack
from datetime import date, datetime, timedelta

SEED = 20261008
WAREHOUSES = 1
DISTRICTS = 10
CUSTOMERS_PER_DISTRICT = 3000
ITEMS = 100000
ORDERS_PER_DISTRICT = 3000
DELIVERED_PER_DISTRICT = 2100

LAST_NAMES = [
    "BARBARBAR", "BARBARULE", "MIDELETHON", "OUGHTISPLY", "UNICOMUND",
    "ANTISOBLE", "BOOMETHED", "EDESDEPEN", "PRESAPRESE", "STEVERTIS",
]
FIRST_SYLLABLES = ["Wes", "Kar", "Ora", "Jil", "Ana", "Sam", "Lia", "Tom", "Eva", "Max"]
ITEM_SYLLABLES = ["bor", "cor", "nis", "phy", "sal", "tan", "ver", "whe", "gold", "iron"]
STATES = ["AZ", "CA", "CO", "FL", "IL", "MA", "NY", "OH", "TX", "WA"]
ALPHA = "abcdefghijklmnopqrstuvwxyz"

BASE_DATE = date(2026, 8, 1)


def money(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def tax4(r, low, high):
    return f"0.{r.randrange(low, high + 1):04d}"


def ts(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def zip9(r):
    return f"{r.randrange(100000):05d}{r.randrange(100):04d}"


def writer(stack, out_dir, name):
    handle = open(os.path.join(out_dir, f"{name}.csv"), "w", newline="")
    stack.enter_context(handle)
    return csv.writer(handle, delimiter="|", lineterminator="\n")


def main(out_dir):
    r = random.Random(SEED)
    os.makedirs(out_dir, exist_ok=True)
    counts = dict.fromkeys(
        ("warehouse", "district", "item", "stock", "customer", "history", "oorder", "new_order", "order_line"), 0
    )

    with ExitStack() as stack:
        warehouses = writer(stack, out_dir, "warehouse")
        districts = writer(stack, out_dir, "district")
        items = writer(stack, out_dir, "item")
        stocks = writer(stack, out_dir, "stock")
        customers = writer(stack, out_dir, "customer")
        histories = writer(stack, out_dir, "history")
        oorders = writer(stack, out_dir, "oorder")
        new_orders = writer(stack, out_dir, "new_order")
        order_lines = writer(stack, out_dir, "order_line")

        for w in range(1, WAREHOUSES + 1):
            warehouses.writerow([
                w, f"WH#{w:03d}", f"Street {w}", f"Avenue {w}", "MetroCity",
                r.choice(STATES), zip9(r), tax4(r, 0, 2000), "300000.00",
            ])
            counts["warehouse"] += 1
            for d in range(1, DISTRICTS + 1):
                districts.writerow([
                    d, w, f"DD#{d:03d}", f"Street {d}", f"Avenue {d}", "MetroCity",
                    r.choice(STATES), zip9(r), tax4(r, 1000, 2000), "30000.00", ORDERS_PER_DISTRICT + 1,
                ])
                counts["district"] += 1

        price_cents = [0] * (ITEMS + 1)
        for i in range(1, ITEMS + 1):
            price = r.randrange(100, 10001)
            price_cents[i] = price
            name = "".join(r.choice(ITEM_SYLLABLES) for _ in range(4))[:24]
            data = "".join(r.choice(ALPHA) for _ in range(40))
            if r.randrange(100) < 10:
                data = data[:20] + "ORIGINAL" + data[28:]
            items.writerow([i, r.randrange(1, 10001), name, money(price), data[:50]])
            counts["item"] += 1

        dist_strings = {}
        for w in range(1, WAREHOUSES + 1):
            for d in range(1, DISTRICTS + 1):
                dist_strings[(w, d)] = (f"SD{w:03d}D{d:02d}" + "".join(r.choice(ALPHA) for _ in range(17)))[:24]
        for w in range(1, WAREHOUSES + 1):
            for i in range(1, ITEMS + 1):
                data = "".join(r.choice(ALPHA) for _ in range(40))
                if r.randrange(100) < 10:
                    data = data[:20] + "ORIGINAL" + data[28:]
                stocks.writerow(
                    [w, i, r.randrange(10, 101)]
                    + [dist_strings[(w, d)] for d in range(1, DISTRICTS + 1)]
                    + ["0.00", r.randrange(10, 101), r.randrange(0, 11), data[:50]]
                )
                counts["stock"] += 1

        for w in range(1, WAREHOUSES + 1):
            for d in range(1, DISTRICTS + 1):
                for c in range(1, CUSTOMERS_PER_DISTRICT + 1):
                    since = ts(
                        datetime.combine(
                            BASE_DATE - timedelta(days=r.randrange(100, 2000)),
                            datetime.min.time(),
                        )
                        + timedelta(seconds=r.randrange(86400))
                    )
                    credit = "BC" if r.randrange(100) < 10 else "GC"
                    customers.writerow([
                        c, d, w, r.choice(FIRST_SYLLABLES) + r.choice(FIRST_SYLLABLES).lower(), "OE",
                        r.choice(LAST_NAMES), f"Street {c}", f"Avenue {c}", "MetroCity",
                        r.choice(STATES), zip9(r),
                        f"1{r.randrange(100):02d}{r.randrange(1000):03d}{r.randrange(10000):04d}",
                        since, credit, "50000.00", tax4(r, 0, 5000), money(r.randrange(-1000, 1001)),
                        "10.00", 1, 0, "c" * 250,
                    ])
                    counts["customer"] += 1
                    h_date = ts(
                        datetime.combine(
                            BASE_DATE - timedelta(days=r.randrange(0, 60)), datetime.min.time()
                        )
                        + timedelta(seconds=r.randrange(86400))
                    )
                    histories.writerow([c, d, w, d, w, h_date, "10.00", f"payment{c:05d}d{d:02d}"])
                    counts["history"] += 1

        for w in range(1, WAREHOUSES + 1):
            for d in range(1, DISTRICTS + 1):
                dist_info = dist_strings[(w, d)]
                for o in range(1, ORDERS_PER_DISTRICT + 1):
                    entry = ts(
                        datetime.combine(
                            BASE_DATE - timedelta(days=r.randrange(0, 60)), datetime.min.time()
                        )
                        + timedelta(seconds=r.randrange(86400))
                    )
                    delivered = o <= DELIVERED_PER_DISTRICT
                    carrier = r.randrange(1, 11) if delivered else ""
                    ol_cnt = r.randrange(5, 16)
                    oorders.writerow([o, d, w, r.randrange(1, CUSTOMERS_PER_DISTRICT + 1), entry, carrier, ol_cnt, 1])
                    counts["oorder"] += 1
                    if not delivered:
                        new_orders.writerow([w, d, o])
                        counts["new_order"] += 1
                    delivery_date = (
                        ts(
                            datetime.combine(BASE_DATE, datetime.min.time())
                            + timedelta(seconds=r.randrange(86400), days=r.randrange(1, 30))
                        )
                        if delivered
                        else ""
                    )
                    for line in range(1, ol_cnt + 1):
                        item_id = r.randrange(1, ITEMS + 1)
                        quantity = r.randrange(1, 11)
                        order_lines.writerow([
                            w, d, o, line, item_id, w, delivery_date, quantity,
                            money(price_cents[item_id] * quantity), dist_info,
                        ])
                        counts["order_line"] += 1

    for name, count in counts.items():
        print(f"{name}: {count}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: tpcc_gen.py <output-dir>")
    main(sys.argv[1])
