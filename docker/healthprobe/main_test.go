package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestCheckPassesOnAHealthyLoopbackEndpoint(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	if err := check(server.URL + "/health"); err != nil {
		t.Fatal(err)
	}
}

func TestCheckFailsOnAnErrorStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer server.Close()
	if err := check(server.URL); err == nil {
		t.Fatal("expected an error for a 503")
	}
}

func TestCheckRefusesANonLoopbackHost(t *testing.T) {
	for _, raw := range []string{"http://example.com/health", "https://127.0.0.1/health", "http://localhost:1/"} {
		if err := check(raw); err == nil {
			t.Fatalf("expected %s to be refused", raw)
		}
	}
}
