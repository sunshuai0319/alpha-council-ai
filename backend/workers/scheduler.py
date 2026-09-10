def main() -> None:
    # The scheduler wiring is added with the trading-cycle service.
    # Keeping this process valid lets Docker health-check the worker skeleton.
    import time

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
