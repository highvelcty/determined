//go:build integration
// +build integration

package db

import (
	"archive/tar"
	"context"
	"crypto/rand"
	"database/sql"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jmoiron/sqlx"
	"github.com/pkg/errors"
	log "github.com/sirupsen/logrus"
	"github.com/uptrace/bun"

	"github.com/shopspring/decimal"
	"github.com/stretchr/testify/require"

	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/structpb"
	"gopkg.in/guregu/null.v3"

	"github.com/determined-ai/determined/master/pkg/archive"
	"github.com/determined-ai/determined/master/pkg/model"
	"github.com/determined-ai/determined/master/pkg/ptrs"
	"github.com/determined-ai/determined/master/pkg/schemas"
	"github.com/determined-ai/determined/master/pkg/schemas/expconf"
	"github.com/determined-ai/determined/proto/pkg/commonv1"
	"github.com/determined-ai/determined/proto/pkg/trialv1"
)

const (
	// RootFromDB returns the relative path from db to root.
	RootFromDB = "../../static/srv"
	// MigrationsFromDB returns the relative path to migrations folder.
	MigrationsFromDB      = "file://../../static/migrations"
	defaultSearcherMetric = "okness"
	// DefaultTestSrcPath returns src to the mnsit_pytorch model example.
	DefaultTestSrcPath = "../../../examples/tutorials/mnist_pytorch"
	// DefaultProjectID is the project used when none is specified.
	DefaultProjectID = 1
)

// Model represents a row from the `models` table. Unused except for tests.
type Model struct {
	Name            string        `db:"name" json:"name"`
	Description     string        `db:"description" json:"description"`
	CreationTime    time.Time     `db:"creation_time" json:"creation_time"`
	LastUpdatedTime time.Time     `db:"last_updated_time" json:"last_updated_time"`
	Metadata        model.JSONObj `db:"metadata" json:"metadata"`
	ID              int           `db:"id" json:"id"`
	Labels          []string      `db:"labels" json:"labels"`
	UserID          model.UserID  `db:"user_id" json:"user_id"`
	Archived        bool          `db:"archived" json:"archived"`
	Notes           string        `db:"notes" json:"notes"`
	WorkspaceID     int           `db:"workspace_id" json:"workspace_id"`
}

func init() {
	testOnlyDBLock = func(sql *sqlx.DB) (unlock func()) {
		tx, err := sql.Beginx()
		if err != nil {
			panic(err)
		}

		const lockID = 0x33ad0708c9bed25b // Chosen arbitrarily.
		if _, err := tx.Exec("SELECT pg_advisory_xact_lock($1)", lockID); err != nil {
			_ = tx.Rollback()
			panic(err)
		}

		return func() {
			if err := tx.Commit(); err != nil {
				_ = tx.Rollback()
				panic(err)
			}
		}
	}
}

// ResolveTestPostgres resolves a connection to a postgres database. To debug tests that use this
// (or otherwise run the tests outside of the Makefile), make sure to set
// DET_INTEGRATION_POSTGRES_URL.
func ResolveTestPostgres() (*PgDB, func(), error) {
	pgDB, err := ConnectPostgres(os.Getenv("DET_INTEGRATION_POSTGRES_URL"))
	if err != nil {
		return nil, nil, fmt.Errorf("failed to connect to postgres: %w", err)
	}
	return pgDB, func() {
		if err := pgDB.Close(); err != nil {
			panic(err)
		}
	}, nil
}

// MustResolveTestPostgres is the same as ResolveTestPostgres but with panics on errors.
func MustResolveTestPostgres(t *testing.T) (*PgDB, func()) {
	pgDB, closeDB, err := ResolveTestPostgres()
	require.NoError(t, err, "failed to connect to postgres")
	return pgDB, closeDB
}

// ResolveNewPostgresDatabase returns a connection to a randomly-named, newly-created database, and
// a function you should defer for deleting it afterwards.
func ResolveNewPostgresDatabase() (*PgDB, func(), error) {
	baseURL := os.Getenv("DET_INTEGRATION_POSTGRES_URL")
	if baseURL == "" {
		return nil, nil, fmt.Errorf("no DET_INTEGRATION_POSTGRES_URL detected")
	}

	url, err := url.Parse(baseURL)
	if err != nil {
		return nil, nil, errors.Wrapf(
			err, "failed to parse DET_INTEGRATION_POSTGRES_URL (%q)", baseURL,
		)
	}

	// Connect to the db server without selecting a database.
	url.Path = ""
	sql, err := sqlx.Connect("pgx", url.String())
	if err != nil {
		return nil, nil, errors.Wrapf(err, "failed to connect to postgres at %q", url)
	}

	randomSuffix := make([]byte, 16)
	_, err = rand.Read(randomSuffix)
	if err != nil {
		return nil, nil, errors.Wrap(err, "failed pick a random name")
	}

	dbname := fmt.Sprintf("intg-%x", randomSuffix)
	_, err = sql.Exec(fmt.Sprintf("CREATE DATABASE %q", dbname))
	if err != nil {
		return nil, nil, errors.Wrapf(err, "failed to create new database %q", dbname)
	}

	// Remember the connection we return to the newly-created database, because if we don't close it
	// we can't drop the database.  When we require postgres>=13, we can use DROP DATABASE ... FORCE
	// instead of manually closing this connection.
	var dbConn *sqlx.DB

	cleanup := func() {
		if dbConn != nil {
			if err := dbConn.Close(); err != nil {
				log.WithError(err).Errorf("failed to close sql")
			}
		}
		if _, err := sql.Exec(fmt.Sprintf("DROP DATABASE %q", dbname)); err != nil {
			log.WithError(err).Errorf("failed to delete temp database %q", dbname)
		}
	}

	success := false

	defer func() {
		if !success {
			cleanup()
		}
	}()

	// Connect to the new database.
	url.Path = fmt.Sprintf("/%v", dbname)
	pgDB, err := ConnectPostgres(url.String())
	if err != nil {
		return nil, nil, errors.Wrapf(err, "failed to connect to new database %q", dbname)
	}

	dbConn = pgDB.sql

	success = true
	return pgDB, cleanup, nil
}

// MustResolveNewPostgresDatabase is the same as ResolveNewPostgresDatabase but panics on errors.
func MustResolveNewPostgresDatabase(t *testing.T) (*PgDB, func()) {
	pgDB, cleanup, err := ResolveNewPostgresDatabase()
	require.NoError(t, err, "failed to create new database")
	return pgDB, cleanup
}

// MustMigrateTestPostgres ensures the integrations DB has migrations applied.
func MustMigrateTestPostgres(t *testing.T, db *PgDB, migrationsPath string, actions ...string) {
	require.NoError(t, MigrateTestPostgres(db, migrationsPath, actions...))
}

// MigrateTestPostgres ensures the integrations DB has migrations applied.
func MigrateTestPostgres(db *PgDB, migrationsPath string, actions ...string) error {
	if len(actions) == 0 {
		actions = []string{"up"}
	}
	err := db.Migrate(
		migrationsPath, strings.ReplaceAll(migrationsPath+"/../views_and_triggers", "file://", ""), actions)
	if err != nil {
		return fmt.Errorf("failed to migrate postgres: %w", err)
	}

	err = InitAuthKeys()
	if err != nil {
		return fmt.Errorf("failed to initAuthKeys: %w", err)
	}
	return nil
}

// MustSetupTestPostgres returns a ready to use test postgres connection.
func MustSetupTestPostgres(t *testing.T) (*PgDB, func()) {
	pgDB, closeDB := MustResolveTestPostgres(t)
	MustMigrateTestPostgres(t, pgDB, MigrationsFromDB)
	return pgDB, closeDB
}

// RequireMockJob returns a stub job.
func RequireMockJob(t *testing.T, db *PgDB, userID *model.UserID) model.JobID {
	jID := model.NewJobID()
	jIn := &model.Job{
		JobID:   jID,
		JobType: model.JobTypeExperiment,
		OwnerID: userID,
		QPos:    decimal.New(0, 0),
	}
	err := AddJobTx(context.TODO(), Bun(), jIn)
	require.NoError(t, err, "failed to add job")
	return jID
}

// RequireMockWorkspaceID returns a mock workspace ID and name.
func RequireMockWorkspaceID(t *testing.T, db *PgDB, wsName string) (int, string) {
	if len(wsName) == 0 {
		wsName = uuid.New().String()
	}

	mockWorkspace := struct {
		bun.BaseModel `bun:"table:workspaces"`

		ID   int `bun:"id,pk,autoincrement"`
		Name string
	}{
		Name: wsName,
	}
	_, err := Bun().NewInsert().Model(&mockWorkspace).Returning("id").Exec(context.TODO())
	require.NoError(t, err)

	return mockWorkspace.ID, mockWorkspace.Name
}

// RequireMockProjectID returns a mock project ID and name.
func RequireMockProjectID(t *testing.T, db *PgDB, workspaceID int, archived bool) (int, string) {
	mockProject := struct {
		bun.BaseModel `bun:"table:projects"`

		ID          int `bun:"id,pk,autoincrement"`
		WorkspaceID int
		Name        string
		Archived    bool
		Description string
	}{
		WorkspaceID: workspaceID,
		Name:        uuid.New().String(),
		Archived:    archived,
		Description: "description text",
	}
	_, err := Bun().NewInsert().Model(&mockProject).Returning("id").Exec(context.TODO())
	require.NoError(t, err)

	return mockProject.ID, mockProject.Name
}

// RequireGetProjectHParams returns projects hparams.
func RequireGetProjectHParams(t *testing.T, db *PgDB, projectID int) []string {
	p := struct {
		bun.BaseModel `bun:"table:projects"`

		Hyperparameters []string
	}{}
	require.NoError(t, Bun().NewSelect().Model(&p).
		Where("id = ?", projectID).
		Scan(context.TODO(), &p))

	return p.Hyperparameters
}

// RequireMockTask returns a mock task.
func RequireMockTask(t *testing.T, db *PgDB, userID *model.UserID) *model.Task {
	jID := RequireMockJob(t, db, userID)

	// Add a task.
	tID := model.NewTaskID()
	tIn := &model.Task{
		TaskID:    tID,
		JobID:     &jID,
		TaskType:  model.TaskTypeTrial,
		StartTime: time.Now().UTC().Truncate(time.Millisecond),
	}
	err := AddTask(context.TODO(), tIn)
	require.NoError(t, err, "failed to add task")
	return tIn
}

// RequireMockUser requires a mock model.
func RequireMockUser(t *testing.T, db *PgDB) model.User {
	user := model.User{
		Username:     uuid.NewString(),
		PasswordHash: null.NewString("", false),
		Active:       true,
	}
	// HACK: to get around user/db import cycle, should have a user.Add().
	_, err := HackAddUser(context.TODO(), &user)
	require.NoError(t, err, "failed to add user")
	return user
}

// RequireMockExperiment returns a mock experiment.
func RequireMockExperiment(t *testing.T, db *PgDB, user model.User) *model.Experiment {
	return RequireMockExperimentParams(t, db, user, MockExperimentParams{}, DefaultProjectID)
}

// RequireMockExperimentProject returns a mock experiment attached to a specific project.
func RequireMockExperimentProject(t *testing.T, db *PgDB, user model.User, projectID int) *model.Experiment {
	return RequireMockExperimentParams(t, db, user, MockExperimentParams{}, projectID)
}

// MockExperimentParams is the parameters for mock experiment.
type MockExperimentParams struct {
	HParamNames          *[]string
	ProjectID            *int
	ExternalExperimentID *string
	State                *model.State
	Integrations         *expconf.IntegrationsConfigV0
}

// RequireMockExperimentParams returns a mock experiment with various parameters.
// nolint: exhaustruct
func RequireMockExperimentParams(
	t *testing.T,
	db *PgDB,
	user model.User,
	p MockExperimentParams,
	projectID int,
) *model.Experiment {
	notDefaulted := expconf.ExperimentConfigV0{
		RawCheckpointStorage: &expconf.CheckpointStorageConfigV0{
			RawSharedFSConfig: &expconf.SharedFSConfigV0{
				RawHostPath: ptrs.Ptr("/home/ckpts"),
			},
		},
		RawEntrypoint: &expconf.EntrypointV0{
			RawEntrypoint: ptrs.Ptr("model.Classifier"),
		},
		RawHyperparameters: map[string]expconf.HyperparameterV0{
			"global_batch_size": {
				RawConstHyperparameter: &expconf.ConstHyperparameterV0{
					RawVal: float64(1),
				},
			},
		},
		RawSearcher: &expconf.SearcherConfigV0{
			RawSingleConfig: &expconf.SingleConfigV0{},
			RawMetric:       ptrs.Ptr(defaultSearcherMetric),
		},
	}
	if p.HParamNames != nil {
		notDefaulted.RawHyperparameters = map[string]expconf.HyperparameterV0{}
		for _, n := range *p.HParamNames {
			notDefaulted.RawHyperparameters[n] = expconf.HyperparameterV0{
				RawConstHyperparameter: &expconf.ConstHyperparameterV0{
					RawVal: ptrs.Ptr(1),
				},
			}
		}
	}
	if p.Integrations != nil {
		notDefaulted.RawIntegrations = p.Integrations
	}

	cfg := schemas.WithDefaults(notDefaulted)

	exp := model.Experiment{
		JobID:                model.NewJobID(),
		State:                model.ActiveState,
		Config:               cfg.AsLegacy(),
		StartTime:            time.Now().Add(-time.Hour).Truncate(time.Millisecond),
		OwnerID:              &user.ID,
		Username:             user.Username,
		ProjectID:            projectID,
		ExternalExperimentID: p.ExternalExperimentID,
	}
	if p.ProjectID != nil {
		exp.ProjectID = *p.ProjectID
	}
	if p.State != nil {
		exp.State = *p.State
	}

	err := db.AddExperiment(&exp, ReadTestModelDefiniton(t, DefaultTestSrcPath), cfg)
	require.NoError(t, err, "failed to add experiment")
	return &exp
}

// ReadTestModelDefiniton reads a test model definition into a []byte.
func ReadTestModelDefiniton(t *testing.T, folderPath string) []byte {
	path, err := filepath.Abs(folderPath)
	require.NoError(t, err)
	files, err := os.ReadDir(path)
	require.NoError(t, err)
	var arcs []archive.Item
	for _, file := range files {
		if file.IsDir() {
			continue
		}
		name := file.Name()
		var bytes []byte
		bytes, err = os.ReadFile(filepath.Join(path, name)) //nolint: gosec
		require.NoError(t, err)
		info, err := file.Info()
		require.NoError(t, err)
		arcs = append(arcs, archive.UserItem(name, bytes, tar.TypeReg, byte(info.Mode()), 0, 0))
	}
	targz, err := archive.ToTarGz(archive.Archive(arcs))
	require.NoError(t, err)
	return targz
}

// RequireMockTrial returns a mock trial.
func RequireMockTrial(t *testing.T, db *PgDB, exp *model.Experiment) (*model.Trial, *model.Task) {
	task := RequireMockTask(t, db, exp.OwnerID)
	rqID := model.NewRequestID(rand.Reader)
	tr := model.Trial{
		RequestID:    &rqID,
		ExperimentID: exp.ID,
		State:        model.ActiveState,
		StartTime:    time.Now(),
		HParams:      model.JSONObj{"global_batch_size": 1},
	}
	err := AddTrial(context.TODO(), &tr, task.TaskID)
	require.NoError(t, err, "failed to add trial")
	return &tr, task
}

// RequireMockTrialID returns a mock trial ID.
func RequireMockTrialID(t *testing.T, db *PgDB, exp *model.Experiment) int {
	trial, _ := RequireMockTrial(t, db, exp)
	return trial.ID
}

// RequireMockAllocation returns a mock allocation.
func RequireMockAllocation(t *testing.T, db *PgDB, tID model.TaskID) *model.Allocation {
	a := model.Allocation{
		AllocationID: model.AllocationID(fmt.Sprintf("%s-1", tID)),
		TaskID:       tID,
		StartTime:    ptrs.Ptr(time.Now().UTC().Truncate(time.Millisecond)),
		State:        ptrs.Ptr(model.AllocationStateTerminated),
	}
	err := AddAllocation(context.TODO(), &a)
	require.NoError(t, err, "failed to add allocation")
	return &a
}

// Option is the return type for WithSteps helper function.
type Option func(f *model.CheckpointV2)

// WithSteps function will add the specified steps to the checkpoint.
func WithSteps(numSteps int) Option {
	return func(f *model.CheckpointV2) {
		f.Metadata = map[string]interface{}{
			"framework":          "some framework",
			"determined_version": "1.0.0",
			"steps_completed":    float64(numSteps),
		}
	}
}

// MockModelCheckpoint returns a mock model checkpoint.
func MockModelCheckpoint(
	ckptUUID uuid.UUID, a *model.Allocation, opts ...Option,
) model.CheckpointV2 {
	stepsCompleted := int32(10)
	ckpt := model.CheckpointV2{
		UUID:         ckptUUID,
		TaskID:       a.TaskID,
		AllocationID: &a.AllocationID,
		ReportTime:   time.Now().UTC(),
		State:        model.CompletedState,
		Resources: map[string]int64{
			"ok": 1.0,
		},
		Metadata: map[string]interface{}{
			"framework":          "some framework",
			"determined_version": "1.0.0",
			"steps_completed":    float64(stepsCompleted),
		},
	}

	for _, opt := range opts {
		opt(&ckpt)
	}

	return ckpt
}

// AddTrialValidationMetrics adds mock Trial Metrics to the database.
func AddTrialValidationMetrics(
	ctx context.Context, ckptUUID uuid.UUID, tr *model.Trial, stepsCompleted int32,
	valMetric int32, pgDB *PgDB,
) error {
	trialMetrics := trialv1.TrialMetrics{
		TrialId:        int32(tr.ID),
		TrialRunId:     int32(0),
		StepsCompleted: &stepsCompleted,
		Metrics: &commonv1.Metrics{
			AvgMetrics: &structpb.Struct{
				Fields: map[string]*structpb.Value{
					"okness": {
						Kind: &structpb.Value_NumberValue{
							NumberValue: float64(valMetric),
						},
					},
				},
			},
		},
	}
	err := pgDB.AddValidationMetrics(ctx, &trialMetrics)
	return err
}

// MustExec allows integration tests to run raw queries directly against a PgDB.
func (db *PgDB) MustExec(t *testing.T, sql string, args ...any) sql.Result {
	out, err := db.sql.Exec(sql, args...)
	require.NoError(t, err, "failed to run query")
	return out
}

// MockWorkspaces creates as many new workspaces as in workspaceNames and
// returns their ids.
func MockWorkspaces(workspaceNames []string, userID model.UserID) ([]int32, error) {
	ctx := context.Background()
	var workspaceIDs []int32
	var workspaces []model.Workspace

	for _, workspaceName := range workspaceNames {
		workspaces = append(workspaces, model.Workspace{
			Name:   workspaceName,
			UserID: userID,
		})
	}

	_, err := Bun().NewInsert().Model(&workspaces).Exec(ctx)
	if err != nil {
		return nil, err
	}

	workspaces = []model.Workspace{}
	err = Bun().NewSelect().Model(&workspaces).
		Where("name IN (?)", bun.In(workspaceNames)).
		Scan(ctx)
	if err != nil {
		return nil, err
	}

	for _, workspace := range workspaces {
		workspaceIDs = append(workspaceIDs, int32(workspace.ID))
	}

	return workspaceIDs, nil
}

// CleanupMockWorkspace removes the specified workspaceIDs from the workspaces table.
func CleanupMockWorkspace(workspaceIDs []int32) error {
	var workspaces []model.Workspace
	_, err := Bun().NewDelete().Model(&workspaces).
		Where("id IN (?)", bun.In(workspaceIDs)).
		Exec(context.Background())

	return err
}
