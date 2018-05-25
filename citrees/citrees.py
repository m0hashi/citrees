from __future__ import division, print_function

from joblib import delayed, Parallel
import numpy as np
from scipy.stats import mode
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.tree import DecisionTreeClassifier
import threading
import warnings
warnings.simplefilter('ignore')

# Package imports
from externals.six.moves import range
from permutation import (permutation_test_dcor, permutation_test_pcor, 
                         permutation_test_dcor_parallel)
from scorers import c_dcor, gini_index, pcor, py_dcor
from utils import bayes_boot_probs, logger

###################
"""SINGLE MODELS"""
###################


class Node(object):
    """Decision node in tree

    Parameters
    ----------
    col : int
        Integer indexing the location of feature or column

    col_pval : float
        Probability value from permutation test for feature selection

    threshold : float
        Best split found in feature 

    impurity : float
        Impurity measuring quality of split

    value : 1d array-like or float
        For classification trees, estimate of each class probability
        For regression trees, central tendency estimate

    left_child : tuple
        For left child node, two element tuple with first element a 2d array of 
        features and second element a 1d array of labels

    right_child : tuple
        For right child node, two element tuple with first element a 2d array of 
        features and second element a 1d array of labels

    Returns
    -------
    None
    """
    def __init__(self, col=None, col_pval=None, threshold=None, impurity=None, 
                 value=None, left_child=None, right_child=None):
        self.col         = col
        self.col_pval    = col_pval
        self.threshold   = threshold
        self.impurity    = impurity
        self.value       = value
        self.left_child  = left_child
        self.right_child = right_child


class CITreeBase(object):
    """Base class for conditional inference tree
    
    Parameters
    ----------
    min_samples_split : int
        Minimum samples required for a split

    alpha : float
        Threshold value for selecting feature with permutation tests. Smaller 
        values correspond to shallower trees

    max_depth : int
        Maximum depth to grow tree

    max_feats : str or int
        Maximum feats to select at each split. String arguments include 'sqrt',
        'log', and 'all'

    n_permutations : int
        Number of permutations during feature selection

    selector : str
        Type of correlation for feature selection, 'pearson' for Pearson
        correlation, 'distance' for distance correlation, or 'hybrid' for
        a dynamic selection of Pearson/distance correlations

    early_stopping : bool
        Whether to implement early stopping during feature selection. If True,
        then as soon as the first permutation test returns a p-value less than
        alpha, this feature will be chosen as the splitting variable

    verbose : bool or int
        Controls verbosity of training and testing

    n_jobs : int
        Number of jobs for permutation testing

    random_state : int
        Sets seed for random number generator
    
    Returns
    -------
    None
    """
    def __init__(self, min_samples_split=2, alpha=.05, max_depth=-1,
                 max_feats=-1, n_permutations=500, selector='pearson', 
                 early_stopping=False, verbose=0, n_jobs=-1, random_state=None):

        # Error checking
        if alpha <= 0 or alpha > 1:
            raise ValueError("Alpha (%.2f) should be in (0, 1]" % alpha)
        if n_permutations < 0:
            raise ValueError("n_permutations (%d) should be > 0" % \
                             n_permutations)
        if selector not in ['distance', 'pearson', 'hybrid']:
            raise ValueError("selector (%s) should be distance, pearson, or " \
                             "hybrid" % selector)
        if max_feats not in ['sqrt', 'log', 'all', -1]:
            raise ValueError("%s not a valid argument for max_feats" % \
                             str(max_feats))

        # Define attributes
        self.alpha             = float(alpha)
        self.min_samples_split = max(1, int(min_samples_split))
        self.n_permutations    = int(n_permutations)
        self.selector          = selector
        self.max_feats         = max_feats
        self.early_stopping    = early_stopping
        self.verbose           = verbose
        self.n_jobs            = n_jobs
        self.root              = None
        self.splitter_counter_ = 0

        if max_depth == -1: 
            self.max_depth = np.inf
        else:
            self.max_depth = int(max(1, max_depth))

        if random_state is None:
            self.random_state = np.random.randint(1, 9999)
        else:
            # TODO: ADD CHECK FOR CRAZY LARGE INTEGER?
            self.random_state = int(random_state)


    def _selector_pcor(self, X, y, col_idx):
        """Selects feature most correlated with y using permutation tests with
        a Pearson correlation
        
        Parameters
        ----------
        X : 2d array-like
            Array of features

        y : 1d array-like
            Array of labels

        col_idx : list
            Columns of X to examine for feature selection
        
        Returns
        -------
        best_col : int
            Best column from feature selection. Note, if early_stopping is 
            enabled then this may not be the absolute best column

        best_pval : float
            Probability value from permutation test
        """
        best_col, best_pval = None, np.inf

        # Iterate over columns
        for col in col_idx:
            pval = permutation_test_pcor(x=X[:, col], 
                                         y=y, 
                                         agg=np.concatenate([X[:, col], y]), 
                                         B=self.n_permutations,
                                         random_state=self.random_state)
            if pval < best_pval: 
                best_col, best_pval = col, pval
                
                # If early stopping
                if self.early_stopping and best_pval < self.alpha:
                    if self.verbose: logger("tree", "Early stopping")
                    return best_col, best_pval

        return best_col, best_pval


    def _selector_dcor(self, X, y, n, col_idx):
        """Selects feature most correlated with y using permutation tests with
        a distance correlation
        
        Parameters
        ----------
        X : 2d array-like
            Array of features

        y : 1d array-like
            Array of labels

        n : int
            Number of samples

        col_idx : list
            Columns of X to examine for feature selection
        
        Returns
        -------
        best_col : int
            Best column from feature selection. Note, if early_stopping is 
            enabled then this may not be the absolute best column

        best_pval : float
            Probability value from permutation test
        """
        best_col, best_pval = None, np.inf

        # Use Numba serial version for 'smaller' samples
        if n < 500:
            for col in col_idx:
                pval = permutation_test_dcor(x=X[:, col], 
                                             y=y, 
                                             agg=np.concatenate([X[:, col], y]), 
                                             n=n,
                                             B=self.n_permutations,
                                             random_state=self.random_state)
                if pval < best_pval: 
                    best_col, best_pval = col, pval

                    # If early stopping
                    if self.early_stopping and best_pval < self.alpha:
                        if self.verbose: logger("tree", "Early stopping")
                        return best_col, best_pval

        else:
            # Use parallel C version for 'larger' samples
            for col in col_idx:
                pval = permutation_test_dcor_parallel(
                                             x=X[:, col], 
                                             y=y, 
                                             agg=np.concatenate([X[:, col], y]), 
                                             n=n,
                                             n_jobs=self.n_jobs,
                                             B=self.n_permutations,
                                             random_state=self.random_state)
                if pval < best_pval: 
                    best_col, best_pval = col, pval

                    # If early stopping
                    if self.early_stopping and best_pval < self.alpha:
                        if self.verbose: logger("tree", "Early stopping")
                        return best_col, best_pval

        return best_col, best_pval


    def _selector_cor_hybrid(self, X, y, n, col_idx):
        """Selects feature most correlated with y using permutation tests with
        a Pearson or a distance correlation, whichever is larger
        
        Parameters
        ----------
        X : 2d array-like
            Array of features

        y : 1d array-like
            Array of labels

        n : int
            Number of samples

        col_idx : list
            Columns of X to examine for feature selection
        
        Returns
        -------
        best_col : int
            Best column from feature selection. Note, if early_stopping is 
            enabled then this may not be the absolute best column

        best_pval : float
            Probability value from permutation test
        """
        best_col, best_pval = None, np.inf

        # Iterate over columns
        for col in col_idx:

            # First calculate correlation to determine which one is larger
            pearson  = abs(pcor(X[:, col], y))
            distance = py_dcor(X[:, col], y, n) if n < 500 else \
                       c_dcor(X[:, col], y, n)
            
            # Calculate permutation test based on correlation values
            if pearson >= distance:
                pval = permutation_test_pcor(x=X[:, col], 
                                             y=y, 
                                             agg=np.concatenate([X[:, col], y]), 
                                             B=self.n_permutations,
                                             random_state=self.random_state)
                if pval < best_pval: 
                    best_col, best_pval = col, pval
                    
                    # If early stopping
                    if self.early_stopping and best_pval < self.alpha:
                        if self.verbose: logger("tree", "Early stopping")
                        return best_col, best_pval

            else:
                # Use Numba serial version for 'smaller' samples
                if n < 500:
                    pval = permutation_test_dcor(x=X[:, col], 
                                                 y=y, 
                                                 agg=np.concatenate([X[:, col], y]), 
                                                 n=n,
                                                 B=self.n_permutations,
                                                 random_state=self.random_state)
                    if pval < best_pval: 
                        best_col, best_pval = col, pval

                        # If early stopping
                        if self.early_stopping and best_pval < self.alpha:
                            if self.verbose: logger("tree", "Early stopping")
                            return best_col, best_pval

                else:
                    # Use parallel C version for 'larger' samples
                    pval = permutation_test_dcor_parallel(
                                                 x=X[:, col], 
                                                 y=y, 
                                                 agg=np.concatenate([X[:, col], y]), 
                                                 n=n,
                                                 n_jobs=self.n_jobs,
                                                 B=self.n_permutations,
                                                 random_state=self.random_state)
                    if pval < best_pval: 
                        best_col, best_pval = col, pval

                        # If early stopping
                        if self.early_stopping and best_pval < self.alpha:
                            if self.verbose: logger("tree", "Early stopping")
                            return best_col, best_pval

        return best_col, best_pval


    def _selector(self, X, y, n, col_idx):
        """Selects feature most correlated with y using permutation tests with
        a correlation function
        
        Parameters
        ----------
        X : 2d array-like
            Array of features

        y : 1d array-like
            Array of labels

        n : int
            Number of samples

        col_idx : list
            Columns of X to examine for feature selection
        
        Returns
        -------
        best_col : int
            Best column from feature selection. Note, if early_stopping is 
            enabled then this may not be the absolute best column

        best_pval : float
            Probability value from permutation test
        """        
        if self.verbose > 1: 
            logger("selector", "Testing %d features" % len(col_idx))

        # Pearson correlation
        if self.selector == 'pearson':
            return self._selector_pcor(X, y, col_idx)

        # Distance correlation 
        elif self.selector == 'distance':
            return self._selector_dcor(X, y, n, col_idx)

        # Hybrid correlation
        else:
            return self._selector_cor_hybrid(X, y, n, col_idx)


    def _splitter(self, *args, **kwargs):
        """Finds best split for feature"""
        raise NotImplementedError("_splitter method not callable from base class")


    def _build_tree(self, X, y, depth=0):
        """Recursively builds tree
        
        Parameters
        ----------
        X : 2d array-like
            Array of features

        y : 1d array-like
            Array of labels

        depth : int
            Depth of current recursive call
        
        Returns
        -------
        Node : object
            Child node or terminal node in recursive splitting
        """
        n, p = X.shape

        # Check for stopping criteria
        if n > self.min_samples_split and depth < self.max_depth:

            # Controls randomness of column sampling (makes analysis reproducible)
            self.splitter_counter_ += 1
            np.random.seed(self.random_state*self.splitter_counter_)

            # Find column with strongest association with outcome
            col_idx       = np.random.choice(np.arange(p, dtype=int),
                                             size=self.max_feats, replace=False)
            col, col_pval = self._selector(X, y, n, col_idx)
            if col_pval < self.alpha:

                # Find best split among selected variable
                impurity, threshold, left, right = self._splitter(X, y, n, col)
                if left and right and len(left[0]) > 0 and len(right[0]) > 0:

                    # Build subtrees for the right and left branches
                    if self.verbose:
                        logger("tree", "Building left subtree with "
                                       "%d samples at depth %d" % \
                                       (len(left[0]), depth+1))
                    left_child = self._build_tree(*left, depth=depth+1)

                    if self.verbose:
                        logger("tree", "Building right subtree with "
                                       "%d samples at depth %d" % \
                                        (len(right[0]), depth+1))
                    right_child = self._build_tree(*right, depth=depth+1)

                    # Return all arguments to constructor except value
                    return Node(col=col, col_pval=col_pval, threshold=threshold,
                                left_child=left_child, right_child=right_child,
                                impurity=impurity) 

        # Calculate terminal node value
        if self.verbose: logger("tree", "Root node reached at depth %d" % depth)
        value = self.node_estimate(y)

        # Terminal node, no other values to pass to constructor
        return Node(value=value)


    def fit(self, X, y=None):
        """Trains model
        
        Parameters
        ----------
        X : 2d array-like
            Array of features

        y : 1d array-like
            Array of labels
        
        Returns
        -------
        self : CITreeBase
            Instance of CITreeBase class
        """
        if self.verbose: 
            logger("tree", "Building root node with %d samples" % X.shape[0])

        # Calculate actual number for max_feats before fitting
        p = X.shape[1]
        if self.max_feats == 'sqrt':
            self.max_feats = int(np.sqrt(p))
        elif self.max_feats == 'log':
            self.max_feats = int(np.log(p+1))
        elif self.max_feats in ['all', -1]:
            self.max_feats = p
        else:
            self.max_feats = int(self.max_feats)
        
        # Begin recursive build
        self.feature_importances_ = np.zeros(X.shape[1])
        self.root                 = self._build_tree(X, y)
        sum_fi                    = np.sum(self.feature_importances_)
        if sum_fi > 0: self.feature_importances_ /= sum_fi
        
        return self


    def predict_label(self, X, tree=None):
        """Predicts label
        
        Parameters
        ----------
        X : 2d array-like
            Array of features for single sample
        
        tree : CITreeBase
            Trained tree

        Returns
        -------
        label : int or float
            Predicted label 
        """
        # If we have a value => return value as the prediction
        if tree is None: tree = self.root
        if tree.value is not None: return tree.value

        # Determine if we will follow left or right branch
        feature_value = X[tree.col]
        branch        = tree.left_child if feature_value <= tree.threshold \
                                        else tree.right_child
        
        # Test subtree
        return self.predict_label(X, branch)


    def predict(self, *args, **kwargs):
        """Predicts labels on test data"""
        raise NotImplementedError("predict method not callable from base class")


    def print_tree(self, tree=None, indent=" ", child=None):
        """Prints tree structure
        
        Parameters
        ----------
        tree : CITreeBase
            Trained tree model

        indent : str
            Indent spacing

        child : Node
            Left or right child node
        
        Returns
        -------
        None
        """
        # If we're at leaf => print the label
        if not tree: tree = self.root
        if tree.value is not None: print("label:", tree.value)
       
        # Go deeper down the tree
        else:
            # Print splitting rule
            print("X[:,%s] %s %s " % (tree.col, 
                                      '<=' if child in [None, 'left'] else '>',
                                      tree.threshold))
            
            # Print the left child
            print("%sL: " % (indent), end="")
            self.print_tree(tree.left_child, indent + indent, 'left')
            
            # Print the right
            print("%sR: " % (indent), end="")
            self.print_tree(tree.right_child, indent + indent, 'right')


class CITreeClassifier(CITreeBase, BaseEstimator, ClassifierMixin):
    """Conditional inference tree classifier

    Derived from CITreeBase class; see constructor for parameter definitions

    Returns
    -------
    None
    """
    def __init__(self,
                 min_samples_split=2, 
                 alpha=.05, 
                 max_depth=-1,
                 max_feats=-1, 
                 n_permutations=100, 
                 selector='pearson', 
                 early_stopping=False, 
                 verbose=0, 
                 n_jobs=-1,
                 random_state=None):

        super(CITreeClassifier, self).__init__(
                    min_samples_split=min_samples_split, 
                    alpha=alpha, 
                    max_depth=max_depth,
                    max_feats=max_feats, 
                    n_permutations=n_permutations, 
                    selector=selector, 
                    early_stopping=early_stopping, 
                    verbose=verbose, 
                    n_jobs=n_jobs,
                    random_state=random_state)


    def _splitter(self, X, y, n, col):
        """Splits data set into two child nodes based on optimized weighted
        gini index
        
        Parameters
        ----------
        X : 2d array-like
            Array of features

        y : 1d array-like
            Array of labels

        n : int
            Number of samples

        col : list
            Column of X to search for best split
        
        Returns
        -------
        best_impurity : float
            Gini index associated with best split

        best_threshold : float
            X value associated with splitting of data set into two child nodes

        left : tuple
            Left child node data consisting of two elements: (features, labels)

        right : tuple
            Right child node data consisting of two elements: (features labels)
        """
        if self.verbose > 1: 
            logger("splitter", "Testing splits on feature %d" % col)
        
        # Call sklearn's optimized implementation of decision tree classifiers
        # to make split using Gini index
        threshold = DecisionTreeClassifier(
                max_depth=1, min_samples_split=self.min_samples_split
            ).fit(X[:, col].reshape(-1, 1), y).tree_.threshold[0]

        # Make split based on best threshold
        idx              = np.where(X[:, col] <= threshold, 1, 0)
        X_left, y_left   = X[idx==1], y[idx==1]
        X_right, y_right = X[idx==0], y[idx==0]
        left, right      = (X_left, y_left), (X_right, y_right)
        n_left, n_right  = len(y_left), len(y_right)

        # Calculate parent and weighted children impurities
        node_impurity  = gini_index(y, self.labels_)
        left_impurity  = gini_index(y_left, self.labels_)*(n_left/float(n))
        right_impurity = gini_index(y_right, self.labels_)*(n_right/float(n))

        # Impurity decrease
        impurity = node_impurity - (left_impurity + right_impurity)

        # Update feature importance (mean decrease impurity)
        self.feature_importances_[col] += impurity

        return impurity, threshold, left, right


    def _estimate_proba(self, y):
        """Estimates class distribution in node
        
        Parameters
        ----------
        y : 1d array-like
            Array of labels
        
        Returns
        -------
        class_probs : 1d array-like
            Array of class probabilities
        """
        return np.array([np.mean(y == label) for label in self.labels_])


    def fit(self, X, y, labels=None):
        """Trains conditional inference tree classifier
        
        Parameters
        ----------
        X : 2d array-like
            Array of features

        y : 1d array-like
            Array of labels

        labels : 1d array-like
            Array of unique class labels
        
        Returns
        -------
        self : CITreeClassifier
            Instance of CITreeClassifier class
        """
        self.labels_       = labels if labels is not None else np.unique(y)
        self.n_classes_    = len(self.labels_)
        self.node_estimate = self._estimate_proba
        super(CITreeClassifier, self).fit(X, y)
        return self


    def predict_proba(self, X):
        """Predicts class probabilities for feature vectors X
        
        Parameters
        ----------
        X : 2d array-like
            Array of features  
        
        Returns
        -------
        class_probs : 2d array-like
            Array of predicted class probabilities
        """
        if self.verbose: 
            logger("test", "Predicting labels for %d samples" % X.shape[0])

        return np.array([self.predict_label(sample) for sample in X])


    def predict(self, X):
        """Predicts class labels for feature vectors X
        
        Parameters
        ----------
        X : 2d array-like
            Array of features  
        
        Returns
        -------
        y : 1d array-like
            Array of predicted classes
        """
        y_proba = self.predict_proba(X)
        return np.argmax(y_proba, axis=1)


#####################
"""ENSEMBLE MODELS"""
#####################


def stratify_sampled_idx(random_state, y, bayes):
    """Indices for stratified bootstrap sampling in classification
    
    Parameters
    ----------
    random_state : int
        Sets seed for random number generator

    y : 1d array-like
        Array of labels

    bayes : bool
        If True, performs Bayesian bootstrap sampling
    
    Returns
    -------
    idx : list
        Stratified sampled indices for each class
    """
    np.random.seed(random_state)
    idx = []
    for label in np.unique(y):

        # Grab indices for class
        tmp = np.where(y==label)[0]

        # Bayesian bootstrapping if specified
        p = bayes_boot_probs(n=len(tmp)) if bayes else None

        idx.append(np.random.choice(tmp, size=len(tmp), replace=True, p=p))

    return idx


def stratify_unsampled_idx(random_state, y, bayes):
    """Unsampled indices for stratified bootstrap sampling in classification
    
    Parameters
    ----------
    random_state : int
        Sets seed for random number generator

    y : 1d array-like
        Array of labels

    bayes : bool
        If True, performs Bayesian bootstrap sampling
    
    Returns
    -------
    idx : list
        Stratified unsampled indices for each class
    """
    np.random.seed(random_state)
    sampled = stratify_sampled_idx(random_state, y, bayes)
    idx     = []
    for i, label in enumerate(np.unique(y)):
        idx.append(np.setdiff1d(np.where(y==label)[0], sampled[i]))
    return idx


def balanced_sampled_idx(random_state, y, bayes, min_class_p):
    """Indices for balanced bootstrap sampling in classification
    
    Parameters
    ----------
    random_state : int
        Sets seed for random number generator

    y : 1d array-like
        Array of labels

    bayes : bool
        If True, performs Bayesian bootstrap sampling

    min_class_p : float
        Minimum proportion of class labels
    
    Returns
    -------
    idx : list
        Balanced sampled indices for each class
    """
    np.random.seed(random_state)
    idx, n = [], int(np.floor(min_class_p*len(y)))
    for i, label in enumerate(np.unique(y)):        
        
        # Grab indices for class
        tmp = np.where(y==label)[0]

        # Bayesian bootstrapping if specified
        p = bayes_boot_probs(n=len(tmp)) if bayes else None

        idx.append(np.random.choice(tmp, size=n, replace=True, p=p))

    return idx


def balanced_unsampled_idx(random_state, y, bayes, min_class_p):
    """Unsampled indices for balanced bootstrap sampling in classification
    
    Parameters
    ----------
    random_state : int
        Sets seed for random number generator

    y : 1d array-like
        Array of labels

    bayes : bool
        If True, performs Bayesian bootstrap sampling

    min_class_p : float
        Minimum proportion of class labels
    
    Returns
    -------
    idx : list
        Balanced unsampled indices for each class
    """
    np.random.seed(random_state)
    sampled = balanced_sampled_idx(random_state, y, bayes, min_class_p)
    idx     = []
    for i, label in enumerate(np.unique(y)):
        idx.append(np.setdiff1d(np.where(y==label)[0], sampled[i]))
    return idx


def normal_sampled_idx(random_state, n, bayes):
    """Indices for bootstrap sampling in classification
    
    Parameters
    ----------
    random_state : int
        Sets seed for random number generator

    y : 1d array-like
        Array of labels

    bayes : bool
        If True, performs Bayesian bootstrap sampling
    
    Returns
    -------
    idx : list
        Sampled indices
    """
    np.random.seed(random_state)

    # Bayesian bootstrapping if specified
    p = bayes_boot_probs(n=n) if bayes else None

    return np.random.choice(np.arange(n, dtype=int), size=n, replace=True, p=p)


def normal_unsampled_idx(random_state, n, bayes):
    """Unsampled indices for bootstrap sampling in classification
    
    Parameters
    ----------
    random_state : int
        Sets seed for random number generator

    y : 1d array-like
        Array of labels

    bayes : bool
        If True, performs Bayesian bootstrap sampling
    
    Returns
    -------
    idx : list
        Unsampled indices
    """
    sampled = normal_sampled_idx(random_state, n, bayes)
    counts  = np.bincount(sampled, minlength=n)
    return np.arange(n, dtype=int)[counts==0]


def _parallel_fit_classifier(tree, X, y, n, tree_idx, n_estimators, bootstrap,
                             bayes, verbose, random_state, class_weight=None,
                             min_dist_p=None): 
    """Utility function for building trees in parallel
    
    Note: This function can't go locally in a class, because joblib complains 
          that it cannot pickle it when placed there
    
    Parameters
    ----------
    tree : CITreeClassifier
        Instantiated conditional inference tree

    X : 2d array-like
        Array of features

    y : 1d array-like
        Array of labels

    n : int
        Number of samples

    tree_idx : int
        Index of tree in forest

    n_estimators : int
        Number of total estimators

    bootstrap : bool
        Whether to perform bootstrap sampling

    bayes : bool
        If True, performs Bayesian bootstrap sampling

    verbose : bool or int
        Controls verbosity of training process

    random_state : int
        Sets seed for random number generator

    class_weight : str
        Type of sampling during bootstrap, None for regular bootstrapping, 
        'balanced' for balanced bootstrap sampling, and 'stratify' for 
        stratified bootstrap sampling

    min_class_p : float
        Minimum proportion of class labels
  
    Returns
    -------
    None
    """
    # Print status if conditions met
    if verbose and n_estimators >= 10:
        denom = n_estimators if verbose > 1 else 10
        if (tree_idx+1) % int(n_estimators/denom) == 0:
            logger("tree", "Building tree %d/%d" % (tree_idx+1, n_estimators))

    # Bootstrap sample if specified
    if bootstrap:
        random_state = random_state*(tree_idx+1)
        if class_weight == 'balanced':
            idx = balanced_sampled_idx(random_state, y, bayes, min_dist_p)
        elif class_weight == 'stratify':
            idx = stratify_sampled_idx(random_state, y, bayes)
        else:
            idx = normal_sampled_idx(random_state, n, bayes)

        # Concatenate and turn into numpy array
        idx = np.concatenate(idx)

        # Note: We need to pass the classes in the case of the bootstrap
        # because not all classes may be sampled and when it comes to prediction,
        # the tree models learns a different number of classes across different
        # bootstrap samples
        tree.fit(X[idx], y[idx], np.unique(y)) 
    else:
        tree.fit(X, y)


def _parallel_fit_regressor():
    """Not implemented
    
    Parameters
    ----------
    
    Returns
    -------
    """
    raise NotImplementedError("Function not implemented currently")


def _accumulate_prediction(predict, X, out, lock):
    """Utility function to aggregate predictions in parallel
    
    Parameters
    ----------
    predict : function handle
        Alias to prediction method of class

    X : 2d array-like
        Array of features

    out : 1d or 2d array-like
        Array of labels

    lock : threading Lock
        A lock that controls worker access to data structures for aggregating
        predictions
    
    Returns
    -------
    None
    """
    prediction = predict(X)
    with lock:
        if len(out) == 1:
            out[0] += prediction
        else:
            for i in range(len(out)): out[i] += prediction[i]


class CIForestBase(object):
    """Base class for conditional inference forests
    
    Parameters
    ----------
    min_samples_split : int
        Minimum samples required for a split

    alpha : float
        Threshold value for selecting feature with permutation tests. Smaller 
        values correspond to shallower trees

    max_depth : int
        Maximum depth to grow tree

    max_feats : str or int
        Maximum feats to select at each split. String arguments include 'sqrt',
        'log', and 'all'

    n_permutations : int
        Number of permutations during feature selection

    selector : str
        Type of correlation for feature selection, 'pearson' for Pearson
        correlation, 'distance' for distance correlation, or 'hybrid' for
        a dynamic selection of Pearson/distance correlations

    early_stopping : bool
        Whether to implement early stopping during feature selection. If True,
        then as soon as the first permutation test returns a p-value less than
        alpha, this feature will be chosen as the splitting variable

    verbose : bool or int
        Controls verbosity of training and testing

    bootstrap : bool
        Whether to perform bootstrap sampling for each tree

    bayes : bool
        If True, performs Bayesian bootstrap sampling

    class_weight : str
        Type of sampling during bootstrap, None for regular bootstrapping, 
        'balanced' for balanced bootstrap sampling, and 'stratify' for 
        stratified bootstrap sampling

    n_jobs : int
        Number of jobs for permutation testing

    random_state : int
        Sets seed for random number generator
    
    Returns
    -------
    None
    """
    def __init__(self, min_samples_split=2, alpha=.05, max_depth=-1,
                 n_estimators=100, max_feats='sqrt', n_permutations=200, 
                 selector='pearson', early_stopping=True, verbose=0, 
                 bootstrap=True, bayes=True, class_weight=None, n_jobs=-1, 
                 random_state=None):

        # Error checking
        if alpha <= 0 or alpha > 1:
            raise ValueError("Alpha (%.2f) should be in (0, 1]" % alpha)
        if n_permutations < 0:
            raise ValueError("n_permutations (%s) should be > 0" % \
                             str(n_permutations))
        if selector not in ['distance', 'pearson', 'hybrid']:
            raise ValueError("selector (%s) should be distance, pearson, or " \
                             "hybrid" % selector)
        if max_feats not in ['sqrt', 'log', 'all', -1]:
            raise ValueError("%s not a valid argument for max_feats" % \
                             str(max_feats))
        if n_estimators < 0:
            raise ValueError("n_estimators (%s) must be > 0" % \
                             str(n_estimators))

        # Only for classifier model
        if class_weight not in [None, 'balanced', 'stratify']:
            raise ValueError("%s not a valid argument for class_weight" % \
                             str(class_weight))
        
        # Placeholder variable for regression model (not applicable)
        if class_weight is None: self.min_class_p = None

        # Define attributes
        self.alpha             = float(alpha)
        self.min_samples_split = max(1, min_samples_split)
        self.n_permutations    = int(n_permutations)
        if max_depth == -1: 
            self.max_depth = max_depth
        else:
            self.max_depth = int(max(1, max_depth))
        self.n_estimators   = int(max(1, n_estimators))
        self.selector       = selector
        self.max_feats      = max_feats
        self.bootstrap      = bootstrap
        self.early_stopping = early_stopping
        self.n_jobs         = n_jobs
        self.verbose        = verbose
        self.class_weight   = class_weight
        self.bayes          = bayes

        if random_state is None:
            self.random_state = np.random.randint(1, 9999)
        else:
            # TODO: ADD CHECK FOR CRAZY LARGE INTEGER?
            self.random_state = int(random_state)

        # Package params for calling CITree{Classifier/Regressor}
        self.params = {
            'alpha': self.alpha, 
            'min_samples_split': self.min_samples_split,
            'n_permutations': self.n_permutations,
            'selector': self.selector,
            'max_feats': self.max_feats,
            'early_stopping': self.early_stopping,
            'verbose': 0,
            'n_jobs': 1,
            'random_state': None,
            }


    def fit(self, X, y):
        """Fit conditional inference forest
        
        Parameters
        ----------
        X : 2d array-like
            Array of features

        y : 1d array-like
            Array of labels
        
        Returns
        -------
        self : CIForestBase
            Instance of CIForestBase
        """
        if self.verbose:
            logger("tree", "Training ensemble with %d trees on %d samples" % \
                    (self.n_estimators, X.shape[0]))

        # Instantiate base tree models
        self.estimators_ = []
        for i in range(self.n_estimators):
            self.params['random_state'] = self.random_state*(i+1)
            self.estimators_.append(self.Tree(**self.params))

        # Define class distribution
        self.class_dist_p = np.array([
                np.mean(y==label) for label in np.unique(y)
            ])

        # Train models
        n = X.shape[0]
        Parallel(n_jobs=self.n_jobs, backend='threading')(
            delayed(self._parallel_fit)(
                self.estimators_[i], X, y, n, i, self.n_estimators, 
                self.bootstrap, self.bayes, self.verbose, self.random_state,
                self.class_weight, np.min(self.class_dist_p)
                ) 
            for i in range(self.n_estimators)
            )

        # Accumulate feature importances (mean decrease impurity)
        self.feature_importances_ = np.sum([
                tree.feature_importances_ for tree in self.estimators_],
                axis=0
            )
        sum_fi = np.sum(self.feature_importances_)
        if sum_fi > 0:
            self.feature_importances_ /= sum_fi

        return self


    def predict(self, *args, **kwargs):
        """Predicts labels on test data"""
        raise NotImplementedError("predict method not callable from base class")


class CIForestClassifier(CIForestBase, BaseEstimator, ClassifierMixin):
    """ADD
    
    Parameters
    ----------
    random_state : int
        Sets seed for random number generator
    
    Returns
    -------
    """
    def __init__(self, 
                 min_samples_split=2, 
                 alpha=.05, 
                 max_depth=-1,
                 n_estimators=100, 
                 max_feats='sqrt', 
                 n_permutations=200, 
                 selector='pearson', 
                 early_stopping=False, 
                 verbose=0, 
                 bootstrap=True,
                 bayes=True,
                 class_weight='balanced',
                 n_jobs=-1, 
                 random_state=None):

        super(CIForestClassifier, self).__init__(
            min_samples_split=min_samples_split, 
            alpha=alpha, 
            max_depth=max_depth,
            n_estimators=n_estimators, 
            max_feats=max_feats, 
            n_permutations=n_permutations, 
            selector=selector, 
            early_stopping=early_stopping, 
            verbose=verbose, 
            bootstrap=bootstrap, 
            bayes=bayes,
            class_weight=class_weight,
            n_jobs=n_jobs, 
            random_state=random_state)


    def fit(self, X, y):
        """Fit conditional inference forest classifier
        
        Parameters
        ----------
        X : 2d array-like
            Array of features

        y : 1d array-like
            Array of labels
        
        Returns
        -------
        self : CIForestClassifier
            Instance of CIForestClassifier
        """
        # Alias to tree model and parallel training function
        self.Tree          = CITreeClassifier
        self._parallel_fit = _parallel_fit_classifier

        # Class information
        self.labels_    = np.unique(y)
        self.n_classes_ = len(self.labels_)
        super(CIForestClassifier, self).fit(X, y)
        return self


    def predict_proba(self, X):
        """Predicts class probabilities for feature vectors X
        
        Parameters
        ----------
        X : 2d array-like
            Array of features  
        
        Returns
        -------
        class_probs : 2d array-like
            Array of predicted class probabilities
        """
        if self.verbose: 
            logger("test", "Predicting labels for %d samples" % X.shape[0])

        # Parallel prediction
        all_proba = np.zeros((X.shape[0], self.n_classes_), dtype=np.float64)
        lock      = threading.Lock()
        Parallel(n_jobs=self.n_jobs, backend="threading")(
            delayed(_accumulate_prediction)(e.predict_proba, X, all_proba, lock)
            for e in self.estimators_)

        # Normalize probabilities
        for proba in all_proba: proba /= len(self.estimators_)
        if len(all_proba) == 1:
            return all_proba[0]
        else:
            return all_proba


    def predict(self, X):
        """Predicts class labels for feature vectors X
        
        Parameters
        ----------
        X : 2d array-like
            Array of features  
        
        Returns
        -------
        y : 1d array-like
            Array of predicted classes
        """
        y_proba = self.predict_proba(X)
        return np.argmax(y_proba, axis=1)

