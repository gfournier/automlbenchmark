import logging
import math
import os
import tempfile as tmp
import warnings

os.environ['JOBLIB_TEMP_FOLDER'] = tmp.gettempdir()
os.environ['OMP_NUM_THREADS'] = '1'
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'
import autosklearn
from autosklearn.estimators import AutoSklearnClassifier, AutoSklearnRegressor
import autosklearn.metrics as metrics
from packaging import version

from frameworks.shared.callee import call_run, result, output_subdir, utils

log = logging.getLogger(__name__)

askl_version = version.parse(autosklearn.__version__)

def run(dataset, config):
    log.info("\n**** AutoSklearn {} ****\n".format(autosklearn.__version__))
    warnings.simplefilter(action='ignore', category=FutureWarning)
    warnings.simplefilter(action='ignore', category=DeprecationWarning)

    is_classification = config.type == 'classification'

    # Mapping of benchmark metrics to autosklearn metrics
    metrics_mapping = dict(
        acc=metrics.accuracy,
        auc=metrics.roc_auc,
        f1=metrics.f1,
        logloss=metrics.log_loss,
        mae=metrics.mean_absolute_error,
        mse=metrics.mean_squared_error,
        rmse=metrics.mean_squared_error if askl_version < version.parse("0.10") else metrics.root_mean_squared_error,
        r2=metrics.r2
    )
    perf_metric = metrics_mapping[config.metric] if config.metric in metrics_mapping else None
    if perf_metric is None:
        # TODO: figure out if we are going to blindly pass metrics through, or if we use a strict mapping
        log.warning("Performance metric %s not supported.", config.metric)

    # Set resources based on datasize
    log.info("Running auto-sklearn with a maximum time of %ss on %s cores with %sMB, optimizing %s.",
             config.max_runtime_seconds, config.cores, config.max_mem_size_mb, perf_metric)
    log.info("Environment: %s", os.environ)

    X_train = dataset.train.X_enc
    y_train = dataset.train.y_enc
    predictors_type = dataset.predictors_type
    log.debug("predictors_type=%s", predictors_type)

    training_params = {k: v for k, v in config.framework_params.items() if not k.startswith('_')}

    n_jobs = config.framework_params.get('_n_jobs', config.cores)
    ml_memory_limit = config.framework_params.get('_ml_memory_limit', 'auto')

    constr_params = {}
    fit_extra_params = {}

    total_memory_mb = utils.system_memory_mb().total
    if ml_memory_limit == 'auto':
        ml_memory_limit = max(
            min(
                config.max_mem_size_mb / n_jobs,
                math.ceil(total_memory_mb / n_jobs)
            ),
            3072  # 3072 is autosklearn default and we use it as a lower bound
        )
    if askl_version >= version.parse("0.11"):
        log.info(
            "Using %sMB memory per job and on a total of %s jobs.",
            ml_memory_limit, n_jobs
        )
        constr_params["memory_limit"] = ml_memory_limit
    else:
        ensemble_memory_limit = config.framework_params.get('_ensemble_memory_limit', 'auto')
        # when memory is large enough, we should have:
        # (cores - 1) * ml_memory_limit_mb + ensemble_memory_limit_mb = config.max_mem_size_mb
        total_memory_mb = utils.system_memory_mb().total
        if ml_memory_limit == 'auto':
            ml_memory_limit = max(min(config.max_mem_size_mb,
                                      math.ceil(total_memory_mb / n_jobs)),
                                  3072)  # 3072 is autosklearn defaults
        if ensemble_memory_limit == 'auto':
            ensemble_memory_limit = max(math.ceil(ml_memory_limit - (total_memory_mb - config.max_mem_size_mb)),
                                        math.ceil(ml_memory_limit / 3),  # default proportions
                                        1024)  # 1024 is autosklearn defaults
        log.info("Using %sMB memory per ML job and %sMB for ensemble job on a total of %s jobs.", ml_memory_limit, ensemble_memory_limit, n_jobs)
        constr_params["ml_memory_limit"] = ml_memory_limit
        constr_params["ensemble_memory_limit"] = ensemble_memory_limit

    log.warning("Using meta-learned initialization, which might be bad (leakage).")
    # TODO: do we need to set per_run_time_limit too?
    estimator = AutoSklearnClassifier if is_classification else AutoSklearnRegressor

    if askl_version >= version.parse("0.8"):
        constr_params['metric'] = perf_metric
    else:
        fit_extra_params['metric'] = perf_metric

    constr_params["time_left_for_this_task"] = config.max_runtime_seconds
    constr_params["n_jobs"] = n_jobs
    constr_params["seed"] = config.seed

    log.info("Auto-sklearn constructor arguments: %s", constr_params)
    log.info("Auto-sklearn fit() arguments: %s", training_params)

    auto_sklearn = estimator(**constr_params, **training_params)
    with utils.Timer() as training:
        auto_sklearn.fit(X_train, y_train, feat_type=predictors_type, **fit_extra_params)

    # Convert output to strings for classification
    log.info("Predicting on the test set.")
    X_test = dataset.test.X_enc
    y_test = dataset.test.y_enc
    predictions = auto_sklearn.predict(X_test)
    probabilities = auto_sklearn.predict_proba(X_test) if is_classification else None

    save_artifacts(auto_sklearn, config)

    return result(output_file=config.output_predictions_file,
                  predictions=predictions,
                  truth=y_test,
                  probabilities=probabilities,
                  target_is_encoded=is_classification,
                  models_count=len(auto_sklearn.get_models_with_weights()),
                  training_duration=training.duration)


def save_artifacts(estimator, config):
    try:
        models_repr = estimator.show_models()
        log.debug("Trained Ensemble:\n%s", models_repr)
        artifacts = config.framework_params.get('_save_artifacts', [])
        if 'models' in artifacts:
            models_file = os.path.join(output_subdir('models', config), 'models.txt')
            with open(models_file, 'w') as f:
                f.write(models_repr)
        if 'debug' in artifacts:
            print('Saving debug artifacts!')
            debug_dir = output_subdir('debug', config)
            ignore_extensions = ['.npy', '.pcs', '.model', '.ensemble', '.pkl']
            tmp_directory = estimator.automl_._backend.temporary_directory
            files_to_copy = []
            for r, d, f in os.walk(tmp_directory):
                for file_name in f:
                    base, ext = os.path.splitext(file_name)
                    if ext not in ignore_extensions:
                        files_to_copy.append(os.path.join(r, file_name))
            for filename in files_to_copy:
                dst = filename.replace(tmp_directory, debug_dir+'/')
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                copyfile(filename, dst)
    except Exception as e:
        log.debug("Error when saving artifacts= {e}.".format(e), exc_info=True)


if __name__ == '__main__':
    call_run(run)
