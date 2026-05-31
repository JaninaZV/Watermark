from dashboard import main

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "portfolio":
        from dashboard.portfolio import portfolio_cli_main
        raise SystemExit(portfolio_cli_main(sys.argv[2:]))
    raise SystemExit(main())
