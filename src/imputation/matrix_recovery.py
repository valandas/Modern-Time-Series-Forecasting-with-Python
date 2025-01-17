from warnings import warn

import numpy as np
from fancyimpute.common import generate_random_column_samples, masked_mae
from fancyimpute.solver import Solver
from numba import njit
from numba.typed import List
from sklearn.utils import check_array
from sklearn.decomposition import TruncatedSVD

#Adapted from https://github.com/eXascaleInfolab/cdrec/blob/main/python/recovery.py
@njit
def interpolate(matrix: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Interpolate missing values column-wise

    Args:
        matrix (np.ndarray): The matrix to be interpolated. Time Series with shape n x m, where n is # of time steps and m is # of time series
        mask (np.ndarray): The mask of nans

    Returns:
        np.ndarray: Interpolated matrix
    """
    n = len(matrix)
    m = len(matrix[0])

    for j in range(0, m):
        mb_start = -1
        prev_value = np.nan
        step = 0  # init

        for i in range(0, n):
            if mask[i][j]:
                # current value is missing - we either start a new block, or we are in the middle of one

                if mb_start == -1:
                    # new missing block
                    mb_start = i
                    mb_end = mb_start + 1

                    while (mb_end < n) and np.isnan(matrix[mb_end][j]):
                        mb_end += 1

                    next_value = np.nan if mb_end == n else matrix[mb_end][j]

                    if mb_start == 0:  # special case #1: block starts with array
                        prev_value = next_value

                    if mb_end == n:  # special case #2: block ends with array
                        next_value = prev_value

                    step = (next_value - prev_value) / (mb_end - mb_start + 1)
                # end if

                matrix[i][j] = prev_value + step * (i - mb_start + 1)
            else:
                # missing block either ended just now or we're traversing normal data
                prev_value = matrix[i][j]
                mb_start = -1
            # end if
        # end for
    # end for

    return matrix


@njit
def centroid_decomposition(matrix: np.ndarray, truncation: int = 0, SV: List = None):
    """Centroid Decomposition, with the optional possibility of specifying truncation or usage of initial sign vectors
       "Memory-efficient Centroid Decomposition for Long Time Series" - https://exascale.info/assets/pdf/khayati_ICDE14.pdf


    Args:
        matrix (np.ndarray): The matrix to be decomposed.
        truncation (int): The number of latent dimensions. Defaults to number of columns
        SV (numba.typing.List): Sign Vector. Defaults to None.

    Returns:
        Tuple(np.ndarray, np.ndarray, List): The decomposed matrices, L and R with dimensions truncation x n and truncation x m, and updated sign vector
    """
    # input processing
    matrix = np.asarray(matrix, dtype=np.float64).copy()
    n = len(matrix)
    m = len(matrix[0])

    if truncation == 0:
        truncation = m

    if truncation < 1 or truncation > m:
        print(
            "[Centroid Decomposition] Error: invalid truncation parameter k="
            + str(truncation)
        )
        print("[Centroid Decomposition] Aboritng decomposition")
        return None

    if SV is None:
        SV = default_SV(n, truncation)

    if len(SV) != truncation:
        print(
            "[Centroid Decomposition] Error: provided list of Sign Vectors doesn't match in size with the truncation truncation parameter k="
            + str(truncation)
        )
        print("[Centroid Decomposition] Aboritng decomposition")
        return None

    L = np.zeros((truncation, n))
    R = np.zeros((truncation, m))

    # main loop - goes up till the truncation param (maximum of which is the # of columns)
    # for j in tqdm(range(0, truncation), leave=False, total=truncation, desc="Centroid Decomposition..."):
    for j in range(0, truncation):
        # calculate the sign vector
        Z = local_sign_vector(matrix, SV[j])

        # calculate the column of R by X^T * Z / ||X^T * Z||
        R_i = matrix.T @ Z
        R_i = R_i / np.linalg.norm(R_i)
        R[j] = R_i

        # calculate the column of L by X * R_i
        L_i = matrix @ R_i
        L[j] = L_i

        # subtract the dimension generated by L_i and R_i from the original matrix
        matrix = matrix - np.outer(L_i, R_i)

        # update the new sign vector in the array
        SV[j] = Z
    # end for

    return (L.T, R.T, SV)


@njit
def local_sign_vector(matrix: np.ndarray, Z: np.ndarray) -> np.ndarray:
    """Algorithm: LSV (Local Sign Vector). Finds locally optimal sign vector Z, i.e.:
    Z being locally optimal means: for all Z' sign vectors s.t. Z' is one sign flip away from Z at some index j,
    we have that ||X^T * Z|| >= ||X^T * Z'||

    Args:
        matrix (np.ndarray): The matrix for which LSV is computed
        Z (np.ndarray): The LSV matrix

    Returns:
        np.ndarray: The LSV matrix
    """
    n = len(matrix)
    m = len(matrix[0])
    eps = np.finfo(np.float64).eps

    Z = local_sign_vector_init(matrix, Z)

    # calculate initial product of X^T * Z with the current version of Z
    direction = matrix.T @ Z
    # calculate initial value of ||X^T * Z||
    lastNorm = np.linalg.norm(direction) ** 2 + eps

    flipped = True

    while flipped:
        # we terminate the loop if during the last pass we didn't flip a single sign
        flipped = False

        for i in range(0, n):
            signDouble = Z[i] * 2
            gradFlip = 0.0

            # calculate how ||X^T * Z|| would change if we would change the sign at position i
            # change to the values of D = X^T * Z is calculated as D_j_new = D_j - 2 * Z_i * M_ij for all j
            for j in range(0, m):
                localMod = direction[j] - signDouble * matrix[i][j]
                gradFlip += localMod * localMod

            # if it results in augmenting ||X^T * Z||
            # flip the sign and replace cached version of X^T * Z and its norm
            if gradFlip > lastNorm:
                flipped = True
                Z[i] = Z[i] * -1
                lastNorm = gradFlip + eps

                for j in range(0, m):
                    direction[j] -= signDouble * matrix[i][j]
                # end for
            # end if
        # end for
    # end while

    return Z


@njit
def local_sign_vector_init(matrix: np.ndarray, Z: np.ndarray) -> np.ndarray:
    """Auxiliary function for LSV to initialize the Z vector:
    Z is initialized sequentiually where at each step we see which sign would give a larger increase to ||X^T * Z||

    Args:
        matrix (np.ndarray): The matrix for which LSV is computed
        Z (np.ndarray): The LSV matrix

    Returns:
        np.ndarray: The LSV matrix
    """
    n = len(matrix)
    m = len(matrix[0])
    direction = matrix[0]

    for i in range(1, n):
        gradPlus = 0.0
        gradMinus = 0.0

        for j in range(0, m):
            localModPlus = direction[j] + matrix[i][j]
            gradPlus += localModPlus * localModPlus
            localModMinus = direction[j] - matrix[i][j]
            gradMinus += localModMinus * localModMinus

        if gradMinus > gradPlus:
            Z[i] = -1

        for j in range(0, m):
            direction[j] += Z[i] * matrix[i][j]

    return Z


@njit
def default_SV(n: int, k: int) -> List:
    """initialize sign vector array with default values

    Args:
        n (int): number of rows in the matrix
        k (int): number of latent dims

    Returns:
        List: numba List
    """
    # default sign vector is (1, 1, ..., 1)^T
    baseZ = np.array([1.0] * n)
    SV = List()

    for _ in range(0, k):
        SV.append(baseZ.copy())

    return SV


class CentroidRecovery(Solver):
    def __init__(
        self,
        truncation: int = 0,
        max_iters: int = 100,
        convergence_threshold: float = 1e-4,
        init_fill_method: str = "interpolate",
        min_value: int = None,
        max_value: int = None,
        verbose: bool = True,
        early_stopping: bool = False,
        early_stopping_tolerance: float = 1e-4,
        early_stopping_patience: int = 5,
    ):
        """A scalable implementation of the Centroid matrix decomposition technique that approximates SVD.
        "Memory-efficient Centroid Decomposition for Long Time Series" - https://exascale.info/assets/pdf/khayati_ICDE14.pdf

        Args:
            truncation (int, optional): Number of latent dimensions. Defaults to 0.
            max_iters (int, optional): Maximum number of iterations to run the imputation. Defaults to 100.
            convergence_threshold (float, optional): The minimum MAE below which optimization terminates. Defaults to 1e-4.
            init_fill_method (str, optional): Defines how the missing values are filled before Centroid Decomposition. Defaults to "interpolate".
                Other options are: "zero", "mean", "median", "min", "random"
            min_value (int, optional): Max value. Defaults to None.
            max_value (int, optional): Min value. Defaults to None.
            verbose (bool, optional): Controls the verbosity. Defaults to True.
            early_stopping (bool, optional): Enables Early Stopping. Defaults to False.
            early_stopping_tolerance (float, optional): Determines a tilerance below which an improvement is not considered to be
                valid in the early stopping algorithm. Defaults to 1e-4.
            early_stopping_patience (int, optional): Number of steps Early Stopping will wait before terminating optimization. Defaults to 5.
        """
        Solver.__init__(
            self, fill_method=init_fill_method, min_value=min_value, max_value=max_value
        )
        assert truncation >= 1, "`truncation` should be greater than or equal to one."
        assert max_iters >= 1, "`max_iters` should be greater than or equal to one."
        self.truncation = truncation
        self.max_iters = max_iters
        self.convergence_threshold = convergence_threshold
        self.verbose = verbose
        self.early_stopping = early_stopping
        self.early_stopping_patience = early_stopping_patience
        self.early_stopping_tolerance = early_stopping_tolerance

    def solve(self, X, missing_mask):
        X = check_array(X, force_all_finite=False)
        n, m = X.shape
        assert (
            self.truncation < m
        ), f"`truncation` ({self.truncation}) should be less than number of columns({m})"

        if missing_mask.sum() == 0:
            warn(
                "[Centroid Recovery] Warning: provided matrix doesn't contain any missing values."
            )
            warn(
                "[Centroid Recovery] The algorithm will run, but will return an unchanged matrix."
            )
        observed_mask = ~missing_mask
        # init persistent values
        SV = default_SV(n, self.truncation)
        iter = 0
        mae = (
            self.convergence_threshold + 1.0
        )  # dummy to ensure it doesn't terminate in 1 hop
        best_score = 1e18  # initializing to a very high MAE
        best_recon = X.copy()
        best_iter = 0
        early_stopping_counter = 0
        # main loop
        while iter < self.max_iters and mae >= self.convergence_threshold:
            # terminated if we reach the interation cap
            # or if our change to missing values from last iteration is small enough
            iter += 1

            # perform truncated decomposition
            res = centroid_decomposition(X, self.truncation, SV)

            if res == None:  # make sure it doesn't fail, if it does - fail as well
                return None
            else:
                (L, R, SV) = res

            # perform a low-rank reconstruction of the original matrix
            recon = np.dot(L, R.T)
            # recon = np.asnumpy(np.dot(L, R.T))
            mae = masked_mae(X_true=X, X_pred=recon, mask=observed_mask)
            # substitute values in the missing blocks with what was reconstructed after truncated CD
            X[missing_mask] = recon[missing_mask]
            X = self.clip(X)
            if self.early_stopping:
                if best_score - mae >= self.early_stopping_tolerance:
                    best_score = mae
                    best_recon = X.copy()
                    best_iter = iter
                    early_stopping_counter = 0
                else:
                    early_stopping_counter += 1
                if early_stopping_counter > self.early_stopping_patience:
                    if self.verbose:
                        print(
                            f"[CentroidRecovery] Early Stopping after {iter+1} iterations. Best iteration is {best_iter+1} with an MAE of {best_score:.6f}"
                        )
                    break
            if self.verbose:
                print(
                    f"[CentroidRecovery] Iter {iter+1}: observed MAE={mae:.6f} Best Iteration: {best_iter+1} Best MAE: {best_score:.6f}"
                )

        if self.verbose:
            print(f"[CentroidRecovery] Iterations Stopped after iteration {iter+1}")
        return best_recon

    def fill(self, X, missing_mask, fill_method=None, inplace=False):
        """
        Parameters
        ----------
        X : np.array
            Data array containing NaN entries

        missing_mask : np.array
            Boolean array indicating where NaN entries are

        fill_method : str
            "zero": fill missing entries with zeros
            "mean": fill with column means
            "median" : fill with column medians
            "min": fill with min value per column
            "random": fill with gaussian samples according to mean/std of column
            "interpolate": linear interpolation for each column (useful for time series)

        inplace : bool
            Modify matrix or fill a copy
        """
        X = check_array(X, force_all_finite=False)

        if not inplace:
            X = X.copy()

        if not fill_method:
            fill_method = self.fill_method

        if fill_method not in (
            "zero",
            "mean",
            "median",
            "min",
            "random",
            "interpolate",
        ):
            raise ValueError("Invalid fill method: '%s'" % (fill_method))
        elif fill_method == "zero":
            # replace NaN's with 0
            X[missing_mask] = 0
        elif fill_method == "mean":
            self._fill_columns_with_fn(X, missing_mask, np.nanmean)
        elif fill_method == "median":
            self._fill_columns_with_fn(X, missing_mask, np.nanmedian)
        elif fill_method == "min":
            self._fill_columns_with_fn(X, missing_mask, np.nanmin)
        elif fill_method == "random":
            self._fill_columns_with_fn(
                X, missing_mask, col_fn=generate_random_column_samples
            )
        elif fill_method == "interpolate":
            interpolate(X, missing_mask)
        return X


class TruncatedSVDImputation(Solver):
    def __init__(
        self,
        rank: int,
        svd_algorithm: str = "arpack",
        init_fill_method: str = "zero",
        min_value: int = None,
        max_value: int = None,
        verbose: bool = True,
    ):
        """Imputing using a simple Truncated SVD

        Args:
            rank (int): Number of latent dimensions.
            svd_algorithm (str, optional): Whether to use random SVD or the default `arpack`.
            init_fill_method (str, optional): Defines how the missing values are filled before Truncated SVD. Defaults to "zero".
                Other options are: "zero", "mean", "median", "min", "random"
            min_value (int, optional): Max value. Defaults to None.
            max_value (int, optional): Min value. Defaults to None.
            verbose (bool, optional): Controls the verbosity. Defaults to True.
        """
        Solver.__init__(
            self, fill_method=init_fill_method, min_value=min_value, max_value=max_value
        )
        assert rank >= 1, "`truncation` should be greater than or equal to one."
        self.rank = rank
        self.svd_algorithm = svd_algorithm
        self.verbose = verbose

    def solve(self, X, missing_mask):
        X = check_array(X, force_all_finite=False)
        n, m = X.shape
        assert (
            self.rank < m
        ), f"`rank` ({self.rank}) should be less than number of columns({m})"

        if missing_mask.sum() == 0:
            warn(
                "[Truncated SVD] Warning: provided matrix doesn't contain any missing values."
            )
            warn(
                "[Truncated SVD] The algorithm will run, but will return an unchanged matrix."
            )
        observed_mask = ~missing_mask
        tsvd = TruncatedSVD(self.rank, algorithm=self.svd_algorithm)
        recon = tsvd.inverse_transform(tsvd.fit_transform(X))
        mae = masked_mae(X_true=X, X_pred=recon, mask=observed_mask)
        X[missing_mask] = recon[missing_mask]
        X = self.clip(X)
        if self.verbose:
            print(f"[Truncated SVD] Truncated SVD complete with MAE: {mae:.6f}")
        return X

# import torch
# from sklearn.utils import check_array
# # from .solver import Solver
# # from .common import masked_mae


# class MatrixFactorization(Solver):
#     def __init__(
#         self,
#         rank=40,
#         learning_rate=0.01,
#         max_iters=50,
#         shrinkage_value=0,
#         min_value=None,
#         max_value=None,
#         verbose=True,
#     ):
#         """
#         Train a matrix factorization model to predict empty
#         entries in a matrix. Mostly copied (with permission) from:
#         https://blog.insightdatascience.com/explicit-matrix-factorization-als-sgd-and-all-that-jazz-b00e4d9b21ea

#         Params
#         =====+
#         rank : (int)
#             Number of latent factors to use in matrix
#             factorization model

#         learning_rate : (float)
#             Learning rate for optimizer

#         max_iters : (int)
#             Number of max_iters to train for

#         shrinkage_value : (float)
#             Regularization term for sgd penalty

#         min_value : float
#             Smallest possible imputed value

#         max_value : float
#             Largest possible imputed value

#         verbose : (bool)
#             Whether or not to printout training progress
#         """
#         Solver.__init__(self, min_value=min_value, max_value=max_value)
#         self.rank = rank
#         self.learning_rate = learning_rate
#         self.max_iters = max_iters
#         self.shrinkage_value = shrinkage_value
#         self._v = verbose

#     @staticmethod    
#     def masked_mae(X_true, X_pred, mask):
#         masked_diff = X_true[mask] - X_pred[mask]
#         return torch.mean(torch.abs(masked_diff))
    
#     def clip(self, X):
#         """
#         Clip values to fall within any global or column-wise min/max constraints
#         """
#         if self.min_value is not None:
#             X[X < self.min_value] = self.min_value
#         if self.max_value is not None:
#             X[X > self.max_value] = self.max_value
#         return X

#     def solve(self, X, missing_mask):
#         """ Train model for max_iters iterations from scratch."""
#         X = torch.from_numpy(check_array(X, force_all_finite=False))
#         if torch.cuda.is_available():
#             device = torch.device("cuda")
#         else:
#             device = torch.device("cpu")
#         # shape data to fit into keras model
#         (n_samples, n_features) = X.shape
#         observed_mask = ~missing_mask
#         training_indices = list(zip(*np.where(observed_mask)))

#         # self.user_vecs = np.random.normal(scale=1.0 / self.rank, size=(n_samples, self.rank))
#         self.user_vecs = torch.randn(n_samples, self.rank, device=device)*1.0 / self.rank
#         # np.random.normal(scale=1.0 / self.rank, size=(n_samples, self.rank))
#         # self.item_vecs = np.random.normal(scale=1.0 / self.rank, size=)
#         self.item_vecs = torch.randn(n_features, self.rank, device=device)*1.0 / self.rank
    

#         self.user_bias = torch.zeros(n_samples, device=device)
#         self.item_bias = torch.zeros(n_features, device=device)
#         self.global_bias = torch.mean(X[observed_mask])

#         for i in range(self.max_iters):
#             # to do: early stopping
#             if (i + 1) % 10 == 0 and self._v:
#                 X_reconstruction = self.clip(self.predict_all())
#                 mae = self.masked_mae(X_true=X, X_pred=X_reconstruction, mask=observed_mask)
#                 print("[MatrixFactorization] Iter %d: observed MAE=%0.6f rank=%d" % (i + 1, mae, self.rank))

#             np.random.shuffle(training_indices)
#             self.sgd(X, training_indices)
#             i += 1

#         X_filled = X.copy()
#         X_filled[missing_mask] = self.clip(self.predict_all()[missing_mask])
#         return X_filled

#     def sgd(self, X, training_indices):
#         # to do: batch learning
#         for (u, i) in training_indices:
#             prediction = self.predict(u, i)
#             e = X[u, i] - prediction  # error

#             # Update biases
#             self.user_bias[u] += self.learning_rate * (e - self.shrinkage_value * self.user_bias[u])
#             self.item_bias[i] += self.learning_rate * (e - self.shrinkage_value * self.item_bias[i])

#             # Update latent factors
#             self.user_vecs[u, :] += self.learning_rate * (
#                 e * self.item_vecs[i, :] - self.shrinkage_value * self.user_vecs[u, :]
#             )
#             self.item_vecs[i, :] += self.learning_rate * (
#                 e * self.user_vecs[u, :] - self.shrinkage_value * self.item_vecs[i, :]
#             )

#     def predict(self, u, i):
#         """ Single user and item prediction."""
#         prediction = self.global_bias + self.user_bias[u] + self.item_bias[i]
#         prediction += self.user_vecs[u, :].dot(self.item_vecs[i, :].T)
#         return prediction

#     def predict_all(self):
#         """ Predict ratings for every user and item."""
#         predictions = self.user_vecs.dot(self.item_vecs.T)
#         predictions += self.global_bias + self.user_bias.unsqueeze(-1) + self.item_bias.unsqueeze(0)
#         return predictions
