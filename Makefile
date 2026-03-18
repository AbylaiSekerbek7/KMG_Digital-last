.PHONY: run test docker-up docker-share docker-share-url docker-down docker-reset docker-logs export-csv

run:
	PYTHONPATH=. python3 -m uvicorn uto_routing.main:app --reload

test:
	PYTHONPATH=. pytest

docker-up:
	docker compose up --build

docker-share:
	docker compose --profile share up --build

docker-share-url:
	python3 scripts/print_share_url.py

docker-down:
	docker compose down

docker-reset:
	docker compose down -v

docker-logs:
	docker compose logs -f

export-csv:
	PYTHONPATH=. python3 scripts/export_sample_csv.py sample_dataset_csv
