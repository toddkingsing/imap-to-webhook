start:
	docker compose up -d

stop:
	docker compose stop

clean:
	docker compose down

build:
	docker compose up -d --build

test:
	docker build --target testing -t imap-to-webhook-test .
