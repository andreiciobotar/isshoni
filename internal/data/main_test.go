package data

import (
	"os"
	"testing"
)

func TestMain(m *testing.M) {
	os.Setenv("ISSHONI_CACHE_DIR", "") // the tests never touch the real session cache
	os.Exit(m.Run())
}
