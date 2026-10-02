"""Stock keeping."""


def restock(stock, item, amount):
    stock[item] = stock.get(item, 0) + amount
    return stock


def remove(stock, item, amount):
    stock[item] = stock.get(item, 0) - amount
    return stock


def total_items(stock):
    return sum(stock.values())


def low_stock(stock, threshold):
    return sorted(item for item, count in stock.items() if count < threshold)
