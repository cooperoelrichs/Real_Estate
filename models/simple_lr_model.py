from sklearn.linear_model import LinearRegression, Ridge
from real_estate.models.price_model import PriceModel


class LinearModel(PriceModel):
    HAS_SIMPLE_COEFS = True
    HAS_FEATURE_IMPORTANCE = False

    def __init__(self, X, y, X_labels):
        self.model = LinearRegression(
            fit_intercept=True,
            normalize=False,
            copy_X=True,
            n_jobs=-1
        )

        self.setup_self(X, y, X_labels)

class RidgeModel(PriceModel):
    HAS_SIMPLE_COEFS = True
    HAS_FEATURE_IMPORTANCE = False
    MODEL_APLHA = 0.1

    def __init__(self, X, y, X_labels):
        self.model = Ridge(
            alpha=self.MODEL_APLHA,
            fit_intercept=True,
            normalize=False,
            copy_X=True,
        )

        self.setup_self(X, y, X_labels)
