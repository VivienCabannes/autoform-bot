# Elementary Number Theory and Algebra

## Chapter 1: Divisibility and Primes

We begin with the most fundamental property of the natural numbers: the well-ordering principle. Every nonempty subset of natural numbers has a least element. This seemingly obvious fact has deep consequences throughout number theory.

### 1.1 Divisibility

**Definition 1.1 (Divisibility).** Let $a, b \in \mathbb{Z}$ with $a \neq 0$. We say that $a$ divides $b$, written $a \mid b$, if there exists an integer $k$ such that $b = ak$.

Divisibility is a partial order on the positive integers. It is reflexive ($a \mid a$), antisymmetric (if $a \mid b$ and $b \mid a$ then $a = \pm b$), and transitive, as the following theorem shows.

**Theorem 1.2 (Transitivity of Divisibility).** Let $a, b, c \in \mathbb{Z}$ with $a \neq 0$ and $b \neq 0$. If $a \mid b$ and $b \mid c$, then $a \mid c$.

*Proof.* Since $a \mid b$, there exists $k_1 \in \mathbb{Z}$ such that $b = a k_1$. Since $b \mid c$, there exists $k_2 \in \mathbb{Z}$ such that $c = b k_2$. Substituting, $c = b k_2 = (a k_1) k_2 = a (k_1 k_2)$. Since $k_1 k_2 \in \mathbb{Z}$, we have $a \mid c$. $\square$

A useful consequence is that divisibility is preserved under linear combinations.

**Theorem 1.3 (Divisibility and Linear Combinations).** If $d \mid a$ and $d \mid b$, then $d \mid (ma + nb)$ for all $m, n \in \mathbb{Z}$.

*Proof.* Write $a = dk_1$ and $b = dk_2$ for some integers $k_1, k_2$. Then $ma + nb = m(dk_1) + n(dk_2) = d(mk_1 + nk_2)$. Since $mk_1 + nk_2 \in \mathbb{Z}$, the result follows. $\square$

### 1.2 The Division Algorithm

The division algorithm is not really an algorithm but an existence and uniqueness theorem. It guarantees that integer division with remainder is always possible.

**Theorem 1.4 (Division Algorithm).** For any integers $a$ and $b$ with $b > 0$, there exist unique integers $q$ (quotient) and $r$ (remainder) such that $a = bq + r$ and $0 \leq r < b$.

*Proof.* Consider the set $S = \{a - bk : k \in \mathbb{Z},\; a - bk \geq 0\}$. This set is nonempty: if $a \geq 0$ take $k = 0$; if $a < 0$ take $k = a$ (then $a - ba = a(1 - b) \geq 0$ since $b \geq 1$). By the well-ordering principle, $S$ has a least element $r = a - bq$ for some $q$.

We claim $r < b$. If $r \geq b$, then $a - b(q+1) = r - b \geq 0$, so $r - b \in S$, contradicting the minimality of $r$.

For uniqueness, suppose $a = bq_1 + r_1 = bq_2 + r_2$ with $0 \leq r_1, r_2 < b$. Then $b(q_1 - q_2) = r_2 - r_1$. Since $|r_2 - r_1| < b$, we must have $q_1 = q_2$ and hence $r_1 = r_2$. $\square$

## Chapter 2: Groups

### 2.1 Basic Definitions

**Definition 2.1 (Group).** A group is a set $G$ together with a binary operation $\cdot : G \times G \to G$ satisfying: (i) associativity: $(a \cdot b) \cdot c = a \cdot (b \cdot c)$; (ii) identity: there exists $e \in G$ such that $e \cdot a = a \cdot e = a$ for all $a$; (iii) inverses: for each $a \in G$ there exists $a^{-1} \in G$ such that $a \cdot a^{-1} = a^{-1} \cdot a = e$.

The identity element of a group is unique: if $e$ and $e'$ are both identities, then $e = e \cdot e' = e'$.

**Theorem 2.2 (Uniqueness of Inverses).** In a group $G$, each element has exactly one inverse.

*Proof.* Suppose $b$ and $c$ are both inverses of $a$. Then $b = b \cdot e = b \cdot (a \cdot c) = (b \cdot a) \cdot c = e \cdot c = c$. $\square$

The following cancellation law is an immediate consequence.

**Theorem 2.3 (Cancellation Law).** In a group $G$, if $a \cdot b = a \cdot c$, then $b = c$. Similarly, if $b \cdot a = c \cdot a$, then $b = c$.

*Proof.* Multiply both sides of $a \cdot b = a \cdot c$ on the left by $a^{-1}$: $a^{-1} \cdot (a \cdot b) = a^{-1} \cdot (a \cdot c)$. By associativity, $(a^{-1} \cdot a) \cdot b = (a^{-1} \cdot a) \cdot c$, so $e \cdot b = e \cdot c$, giving $b = c$. The right cancellation is proved symmetrically. $\square$
