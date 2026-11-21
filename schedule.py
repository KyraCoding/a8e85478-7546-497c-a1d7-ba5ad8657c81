def distribute(numbers, millis):
    ordered = sorted(numbers, key=lambda n: (-millis[n], n))
    if not ordered:
        return []
    total = sum(millis[n] for n in ordered)
    largest = millis[ordered[0]]
    best_cost = total + 2000
    best = [ordered]
    for count in range(2, min(len(ordered), 8) + 1):
        lower_bound = max(largest, (total + count - 1) // count)
        if lower_bound + 2000 * count >= best_cost:
            continue
        workers = [[] for _ in range(count)]
        loads = [0] * count
        for number in ordered:
            worker = loads.index(min(loads))
            workers[worker].append(number)
            loads[worker] += millis[number]
        cost = max(loads) + 2000 * count
        if cost < best_cost:
            best, best_cost = workers, cost
    return best
